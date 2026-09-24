"""Engine — drives invoices from accept through to delivery.

Each accepted invoice gets its own async task: the process loop. It asks the planner
what to do, runs those operations, and applies the movements they report to the
invoice's holdings; if an operation fails it re-plans from the holdings. It knows
nothing about chains, tokens or gas (see docs/EXECUTION.md).

It also takes x402 payments for jobs funded that way: settling the payment through
a facilitator is what delivers the job's input, so it happens before the job starts.

The reaper runs alongside and expires stale quotes.
"""

import asyncio
import logging

from eth_utils.address import to_checksum_address

from ..blockchain.chains import Chain
from ..blockchain.evm import EvmChain
from ..blockchain.keys import ChainKey
from ..common import TeeSwapError
from ..facilitator import FacilitatorMonitor
from ..http import BaseHttpClient, HttpClient, RecordingClient
from ..types import (
    AcceptResponse,
    Amount,
    Balance,
    Hex32,
    KeysQuoteRequest,
    QuoteRequest,
    QuoteResponse,
    Token,
    TokenAmount,
    Url,
)
from ..x402 import (
    PaymentPayload,
    PaymentRequirements,
    SettleResponse,
    X402PaymentResult,
    X402PaymentSpec,
)
from .accounts import Accounts
from .invoice import (
    Funding,
    Handover,
    Invoice,
    InvoiceId,
    InvoiceRegistry,
    InvoiceStateError,
    InvoiceStatus,
    ReleasedAccount,
)
from .operations import Operation, OperationContext, ReceivePayment
from .planner import FacilitatorsFor, Finished, NoRouteError, decide
from .quote import QuoteError, compute_quote
from .worklog import Step, StepReport, StepStatus

logger = logging.getLogger(__name__)

REAPER_INTERVAL = 60.0
# passes that ran operations but moved nothing, before the engine halts the job
MAX_STALLED_PASSES = 3
STALL_BACKOFF_SECONDS = 5.0
X402_SCHEME = "exact"
# how long a client's payment authorization must stay valid for Mai to settle it
PAYMENT_TIMEOUT_SECONDS = 60


class GuardrailError(TeeSwapError):
    """Something is truly broken (not merely unsuccessful): the engine halts the job."""


class X402SettlementError(TeeSwapError):
    """No facilitator settled the payment: the client may pay again."""


class Engine:
    def __init__(
        self,
        registry: InvoiceRegistry,
        root_key: bytes,
        rpc_urls: dict[str, Url],
        facilitators: FacilitatorMonitor,
    ) -> None:
        self._registry = registry
        self._root_key = root_key
        self._rpc_urls = rpc_urls
        self._facilitators = facilitators
        self._reaper_task: asyncio.Task[None] | None = None

    def rpc_url_for_chain(self, chain: Chain) -> Url:
        url = self._rpc_urls.get(chain.caip2)
        if url is None:
            raise NoRouteError(f"no RPC configured for {chain.name} ({chain.caip2})")
        return url

    def facilitators_for(self, chain: Chain, excluded: set[str]) -> tuple[str, ...]:
        return self._facilitators.candidates(X402_SCHEME, chain.caip2, excluded)

    def _facilitators_for_job(self, invoice: Invoice) -> FacilitatorsFor:
        return lambda chain: self.facilitators_for(chain, invoice.excluded_facilitators)

    async def quote(self, request: QuoteRequest, funding: Funding) -> QuoteResponse:
        """Quote the request and hold it as an invoice, ready to accept."""
        quote_id = InvoiceId.generate()
        accounts = Accounts.for_job(self._root_key, quote_id)
        async with HttpClient() as client:
            return await self._quote(client, request, quote_id, accounts, funding)

    async def quote_keys(self, request: KeysQuoteRequest) -> QuoteResponse:
        """Quote a job whose inputs are already in accounts the client holds the keys to.

        The accounts become the job's input accounts, and what each holds now is its
        input. The keys stay the client's too: spending from an account while Mai works
        is theirs to decide, and the job copes with whatever the chain then shows.
        """
        tokens = [held.token for held in request.inputs]
        if len(set(tokens)) != len(tokens):
            raise QuoteError("one account per input token")
        quote_id = InvoiceId.generate()
        accounts = Accounts.for_job(self._root_key, quote_id)
        async with HttpClient() as client:
            inputs: list[TokenAmount] = []
            for i, held in enumerate(request.inputs):
                chain = held.token.chain
                account = accounts.adopt(chain, f"input/{i}", held.private_key.to_bytes())
                evm = EvmChain(chain, client, self.rpc_url_for_chain(chain))
                owner = to_checksum_address(account.address.value)
                balance = await evm.token_balance(held.token, owner)
                inputs.append(TokenAmount(token=held.token, amount=Amount(balance)))
            quote_request = QuoteRequest(
                inputs=tuple(inputs),
                outputs=request.outputs,
                tolerance_percent=request.tolerance_percent,
            )
            return await self._quote(client, quote_request, quote_id, accounts, Funding.KEYS)

    async def _quote(
        self,
        client: BaseHttpClient,
        request: QuoteRequest,
        quote_id: InvoiceId,
        accounts: Accounts,
        funding: Funding,
    ) -> QuoteResponse:
        quote = await compute_quote(
            client,
            self.rpc_url_for_chain,
            request,
            quote_id,
            accounts,
            funding,
            lambda chain: self.facilitators_for(chain, set()),
        )
        self._registry.create_quote(request, quote, accounts, funding)
        return quote

    def accept(self, invoice_id: InvoiceId) -> AcceptResponse:
        invoice = self._registry.get(invoice_id)
        invoice.require_funding(Funding.DEPOSIT, Funding.KEYS)
        invoice.accept()
        self._start(invoice)
        if invoice.funding == Funding.KEYS:
            instructions = "Nothing to send: the inputs are in the accounts you handed over"
        else:
            instructions = "; ".join(
                f"Send {d.amount.amount} {d.amount.token.symbol} to {d.address.value} "
                f"on {d.address.chain.name}"
                for d in invoice.deposits
            )
        return AcceptResponse(
            quote_id=str(invoice.id),
            deposits=invoice.deposits,
            expires_at=invoice.expires_at,
            instructions=instructions,
        )

    # --- Tools down ---

    def tools_down(self, invoice_id: InvoiceId) -> None:
        """The owner asks Mai to stop. A running job stops once nothing is in flight."""
        invoice = self._registry.get(invoice_id)
        if invoice.status == InvoiceStatus.TOOLS_DOWN:
            return
        if invoice.is_active:
            invoice.tools_down_requested = True  # the planner puts them down next
        else:
            invoice.finish(InvoiceStatus.TOOLS_DOWN, "the owner put the tools down")

    async def hand_over(self, invoice_id: InvoiceId) -> Handover:
        """With the tools down: every account the job controls, its key, and what the chain
        shows it holding now. Only ever returned to the owner over an encrypted reply."""
        invoice = self._registry.get(invoice_id)
        if invoice.status != InvoiceStatus.TOOLS_DOWN:
            raise InvoiceStateError("the tools aren't down: put them down first")
        released: list[ReleasedAccount] = []
        async with HttpClient() as client:
            for account in invoice.accounts.all:
                chain = account.address.chain
                evm = EvmChain(chain, client, self.rpc_url_for_chain(chain))
                owner = to_checksum_address(account.address.value)
                # the chain's native token (gas), then the job's own tokens on this chain
                native = Token.native(chain)
                tokens = [native] + [
                    t.token
                    for t in invoice.quote.inputs
                    if t.token.chain == chain and t.token != native
                ]
                balances = [
                    TokenAmount(token=t, amount=Amount(await evm.token_balance(t, owner)))
                    for t in tokens
                ]
                for amount in balances:
                    invoice.observed(Balance(amount=amount, address=account.address))
                key = invoice.accounts.key(account.address, ChainKey)
                released.append(
                    ReleasedAccount(
                        address=account.address,
                        purpose=account.purpose,
                        private_key=Hex32.from_bytes(key.private_key),
                        balances=tuple(balances),
                    )
                )
        invoice.release()
        return Handover(status=invoice.status, reason=invoice.reason, accounts=tuple(released))

    # --- x402 funding ---

    async def payment_spec(self, invoice_id: InvoiceId, error: str) -> X402PaymentSpec:
        """What paying for this invoice takes: its input, paid to the job's address."""
        invoice = self._registry.get(invoice_id)
        invoice.require_funding(Funding.X402)
        async with HttpClient() as client:
            requirements = await self._payment_requirements(client, invoice)
        return X402PaymentSpec(error=error, accepts=(requirements,))

    async def accept_x402(
        self, invoice_id: InvoiceId, payment: PaymentPayload
    ) -> X402PaymentResult:
        """Receive the client's payment into the input's account, then start the job.

        Returns only once the payment is final: the funds are there, or the payment can
        never be used. Until then it may still settle, so saying it failed could get
        the client to pay twice.
        """
        invoice = self._registry.get(invoice_id)
        invoice.require_funding(Funding.X402)
        async with invoice.payment_lock:
            invoice.check_acceptable()
            (deposit,) = invoice.deposits
            async with HttpClient() as client:
                requirements = await self._payment_requirements(client, invoice)
            if payment.accepted != requirements:
                raise X402SettlementError("payment does not match the payment requirements")
            chain = deposit.address.chain
            receive = ReceivePayment(
                deposit,
                payment,
                requirements,
                self.facilitators_for(chain, invoice.excluded_facilitators),
            )
            step = await self._run(invoice, receive)
            if receive.settlement is None:
                raise X402SettlementError(step.error or "payment not received")
            invoice.begin()
            self._start(invoice)
        response = AcceptResponse(
            quote_id=str(invoice.id),
            deposits=invoice.deposits,
            expires_at=invoice.expires_at,
            instructions=_paid_instructions(receive.settlement),
        )
        return X402PaymentResult(response=response, settlement=receive.settlement)

    async def _payment_requirements(
        self, client: BaseHttpClient, invoice: Invoice
    ) -> PaymentRequirements:
        (deposit,) = invoice.deposits  # the planner routes exactly one input token
        token = deposit.amount.token
        if token.contract is None:
            raise NoRouteError(f"x402 pays in tokens, not native {token.symbol}")
        chain = deposit.address.chain
        name, version = await EvmChain(chain, client, self.rpc_url_for_chain(chain)).eip712_domain(
            token
        )
        return PaymentRequirements(
            scheme=X402_SCHEME,
            network=chain.caip2,
            amount=deposit.amount.amount,
            asset=to_checksum_address(str(token.contract)),
            payTo=deposit.address.value,
            maxTimeoutSeconds=PAYMENT_TIMEOUT_SECONDS,
            extra={"name": name, "version": version},
        )

    def _start(self, invoice: Invoice) -> None:
        invoice.task = asyncio.create_task(self._run_invoice(invoice))

    async def _run_invoice(self, invoice: Invoice) -> None:
        try:
            await self._drive(invoice)
        except Exception as e:
            # a bug, or books that don't add up: stop moving money, leave everything where
            # it is, and hand it to the owner
            logger.exception("invoice %s: halted", invoice.id.ref)
            invoice.finish(InvoiceStatus.TOOLS_DOWN, f"halted: {type(e).__name__}: {e}")

    async def _drive(self, invoice: Invoice) -> None:
        """Run what the planner schedules; when it's done, or a step of it fails, ask the
        planner again. The planner decides when the job is finished. The engine only
        halts it when something is truly broken (see GuardrailError)."""
        stalled = 0
        while True:
            decision = decide(invoice, self._facilitators_for_job(invoice))
            if isinstance(decision, Finished):
                invoice.finish(decision.status, decision.reason)
                return
            if not decision.operations:
                raise GuardrailError("the job isn't finished, but there is nothing to do")
            moved = len(invoice.holdings.movements)
            for operation in decision.operations:
                step = await self._run(invoice, operation)
                if step.status != StepStatus.COMPLETED:
                    break  # the rest of the plan may depend on it: ask the planner again
            if len(invoice.holdings.movements) > moved:
                stalled = 0
                continue
            stalled += 1
            if stalled >= MAX_STALLED_PASSES:
                raise GuardrailError(f"no progress in {stalled} passes")
            await asyncio.sleep(STALL_BACKOFF_SECONDS * stalled)

    async def _run(self, invoice: Invoice, operation: Operation) -> Step:
        """Run one operation on a new work-log step; the step, as the operation left it."""
        invoice.status = operation.phase
        action = invoice.worklog.start(operation.description, operation.chain)
        step = action.add_step(type(operation).__name__)
        step.start()
        async with RecordingClient(step.http_exchanges) as client:
            ctx = OperationContext(
                invoice=invoice,
                accounts=invoice.accounts,
                step=StepReport(step),
                client=client,
                rpc_url_for=self.rpc_url_for_chain,
                report=invoice.holdings.apply,
                exclude_facilitator=invoice.excluded_facilitators.add,
            )
            await operation.run(ctx)
        if not step.finished:
            raise GuardrailError(f"{step.id}: {step.operation} returned without reporting its end")
        return step

    # --- Reaper ---

    def start_reaper(self) -> None:
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reap_loop())

    def stop_reaper(self) -> None:
        if self._reaper_task is not None and not self._reaper_task.done():
            self._reaper_task.cancel()

    async def _reap_loop(self) -> None:
        while True:
            try:
                count = self._registry.reap_expired()
                if count > 0:
                    logger.info("reaped %d expired quotes", count)
            except Exception:
                logger.exception("reaper failed")
            await asyncio.sleep(REAPER_INTERVAL)


def _paid_instructions(settlement: SettleResponse) -> str:
    if settlement.transaction:
        return f"Paid by x402 in transaction {settlement.transaction}"
    return "Paid by x402 (the funds arrived; no facilitator reported the transaction)"
