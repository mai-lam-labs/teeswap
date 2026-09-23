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

from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address

from .. import facilitator
from ..blockchain.chains import Chain
from ..blockchain.evm import EthSigner, EvmChain
from ..common import TeeSwapError
from ..facilitator import FacilitatorError, FacilitatorMonitor
from ..http import BaseHttpClient, HttpClient, RecordingClient
from ..types import AcceptResponse, QuoteRequest, QuoteResponse, Timestamp, TxHash, Url
from ..x402 import (
    PaymentPayload,
    PaymentRequirements,
    SettleResponse,
    X402PaymentResult,
    X402PaymentSpec,
)
from .invoice import (
    Action,
    Funding,
    Invoice,
    InvoiceExpiredError,
    InvoiceId,
    InvoiceRegistry,
    InvoiceStatus,
)
from .ledger import Movement, MovementKind
from .operations import Operation, OperationContext
from .planner import FacilitatorsFor, NoRouteError, plan
from .quote import compute_quote

logger = logging.getLogger(__name__)

REAPER_INTERVAL = 60.0
MAX_REPLANS = 3
X402_SCHEME = "exact"
# how long a client's payment authorization must stay valid for Mai to settle it
PAYMENT_TIMEOUT_SECONDS = 300


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

    def _signer(self, invoice_id: InvoiceId) -> EthSigner:
        return EthSigner.derive(self._root_key, invoice_id.encode())

    def deposit_address(self, invoice_id: InvoiceId) -> ChecksumAddress:
        return self._signer(invoice_id).address

    async def quote(self, request: QuoteRequest, funding: Funding) -> QuoteResponse:
        """Quote the request and hold it as an invoice, ready to accept."""
        quote_id = InvoiceId.generate()
        async with HttpClient() as client:
            quote = await compute_quote(
                client,
                self.rpc_url_for_chain,
                request,
                quote_id,
                self.deposit_address(quote_id),
                funding,
                lambda chain: self.facilitators_for(chain, set()),
            )
        self._registry.create_quote(request, quote, self._signer(quote_id), funding)
        return quote

    def accept(self, invoice_id: InvoiceId) -> AcceptResponse:
        invoice = self._registry.get(invoice_id)
        invoice.require_funding(Funding.DEPOSIT)
        invoice.accept()
        self._start(invoice)
        return AcceptResponse(
            quote_id=str(invoice.id),
            deposits=invoice.deposits,
            expires_at=invoice.expires_at,
            instructions="; ".join(
                f"Send {d.amount.amount} {d.amount.token.symbol} to {d.address.value} "
                f"on {d.address.chain.name}"
                for d in invoice.deposits
            ),
        )

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
        """Settle the client's payment, which delivers the job's input, then start the job."""
        invoice = self._registry.get(invoice_id)
        invoice.require_funding(Funding.X402)
        async with invoice.payment_lock:
            invoice.check_acceptable()
            (deposit,) = invoice.deposits
            async with HttpClient() as client:
                requirements = await self._payment_requirements(client, invoice)
                if payment.accepted != requirements:
                    raise X402SettlementError("payment does not match the payment requirements")
                settlement = await self._settle(client, invoice, payment, requirements)
            invoice.holdings.apply(
                Movement(
                    kind=MovementKind.INPUT,
                    amount=deposit.amount,
                    source=None,
                    destination=invoice.held(deposit.address.chain),
                    timestamp=Timestamp.now(),
                    transaction=TxHash(settlement.transaction),
                )
            )
            invoice.begin()
            self._start(invoice)
        response = AcceptResponse(
            quote_id=str(invoice.id),
            deposits=invoice.deposits,
            expires_at=invoice.expires_at,
            instructions=f"Paid by x402 in transaction {settlement.transaction}",
        )
        return X402PaymentResult(response=response, settlement=settlement)

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

    async def _settle(
        self,
        client: BaseHttpClient,
        invoice: Invoice,
        payment: PaymentPayload,
        requirements: PaymentRequirements,
    ) -> SettleResponse:
        """Try the job's facilitators in order. One that errors or fails to settle is
        excluded from the job; one that finds the payment invalid is not (it's the payment)."""
        (deposit,) = invoice.deposits
        failures: list[str] = []
        for url in self.facilitators_for(deposit.address.chain, invoice.excluded_facilitators):
            try:
                verified = await facilitator.verify(client, url, payment, requirements)
                if not verified.isValid:
                    failures.append(f"{url}: invalid payment: {verified.invalidReason}")
                    continue
                settled = await facilitator.settle(client, url, payment, requirements)
            except FacilitatorError as e:
                invoice.excluded_facilitators.add(url)
                failures.append(str(e))
                continue
            if settled.success:
                return settled
            invoice.excluded_facilitators.add(url)
            failures.append(f"{url}: not settled: {settled.errorReason or settled.errorMessage}")
        raise X402SettlementError("; ".join(failures) or "no facilitator available")

    def _start(self, invoice: Invoice) -> None:
        invoice.task = asyncio.create_task(self._run_invoice(invoice))

    async def _run_invoice(self, invoice: Invoice) -> None:
        """The process loop: plan, run operations, apply what they report; re-plan on failure."""
        replans = 0
        while True:
            try:
                operations = plan(invoice, self._facilitators_for_job(invoice))
            except NoRouteError:
                logger.exception("invoice %s: no route left", invoice.id)
                invoice.status = InvoiceStatus.FAILED
                return
            if not operations:
                invoice.status = InvoiceStatus.DELIVERED
                return
            try:
                for operation in operations:
                    await self._run_operation(invoice, operation)
            except InvoiceExpiredError:
                logger.info("invoice %s expired awaiting deposit", invoice.id)
                invoice.status = InvoiceStatus.EXPIRED
                return
            except Exception:
                # re-evaluate: operations run one at a time, so nothing is still in flight here
                replans += 1
                logger.exception("invoice %s: operation failed (re-plan %d)", invoice.id, replans)
                if replans > MAX_REPLANS:
                    invoice.status = InvoiceStatus.FAILED
                    return

    async def _run_operation(self, invoice: Invoice, operation: Operation) -> None:
        invoice.status = operation.phase
        action = Action(
            description=operation.description,
            source_chain=operation.chain,
            destination_chain=None,
        )
        invoice.actions.append(action)
        step = action.add_step(type(operation).__name__)
        step.start()
        try:
            async with RecordingClient(step.http_exchanges) as client:
                ctx = OperationContext(
                    invoice=invoice,
                    signer=invoice.signer,
                    step=step,
                    client=client,
                    rpc_url_for=self.rpc_url_for_chain,
                    report=invoice.holdings.apply,
                    exclude_facilitator=invoice.excluded_facilitators.add,
                )
                await operation.run(ctx)
        except Exception as e:
            step.fail(str(e))
            raise
        step.complete()

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
