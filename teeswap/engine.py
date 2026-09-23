"""Engine — drives invoices from accept through to delivery.

Each accepted invoice gets its own async task. The task polls for the deposit,
executes the transfers, and records everything on the work log.

The reaper runs alongside and expires stale quotes.
"""

import asyncio
import logging
from dataclasses import replace

from .blockchain.chains import Chain
from .blockchain.evm import EthSigner, EvmRpcClient
from .blockchain.rpc import jsonrpc
from .http import HttpClient, RecordingClient
from .invoice import (
    Action,
    InputStatus,
    Invoice,
    InvoiceExpiredError,
    InvoiceId,
    InvoiceRegistry,
    InvoiceStatus,
    OutputStatus,
)
from .types import (
    AcceptResponse,
    Amount,
    QuoteRequest,
    QuoteResponse,
    Timestamp,
    Transaction,
    TxHash,
    Url,
)

logger = logging.getLogger(__name__)

DEPOSIT_POLL_INTERVAL = 5.0
REAPER_INTERVAL = 60.0
ETH_TRANSFER_GAS = 21_000


class Engine:
    def __init__(
        self,
        registry: InvoiceRegistry,
        root_key: bytes,
        rpc_urls: dict[str, Url],
    ) -> None:
        self._registry = registry
        self._root_key = root_key
        self._rpc_urls = rpc_urls
        self._reaper_task: asyncio.Task[None] | None = None

    def rpc_url_for_chain(self, chain: Chain) -> Url:
        url = self._rpc_urls.get(chain.caip2)
        if url is None:
            raise ValueError(f"no RPC URL configured for {chain.name} ({chain.caip2})")
        return url

    def create_invoice(self, request: QuoteRequest, quote: QuoteResponse) -> Invoice:
        signer = EthSigner.derive(self._root_key, quote.quote_id.encode())
        return self._registry.create_quote(request, quote, signer)

    def accept(self, invoice_id: InvoiceId) -> AcceptResponse:
        invoice = self._registry.get(invoice_id)
        invoice.accept()

        invoice.task = asyncio.create_task(self._run_invoice(invoice))

        deposits = tuple(inp.deposit for inp in invoice.inputs)
        return AcceptResponse(
            quote_id=str(invoice.id),
            deposits=deposits,
            expires_at=invoice.expires_at,
            instructions="; ".join(
                f"Send {d.amount.amount} {d.amount.token.symbol} to {d.address.value} "
                f"on {d.address.chain.name}"
                for d in deposits
            ),
        )

    async def _run_invoice(self, invoice: Invoice) -> None:
        signer = invoice.signer
        try:
            for index in range(len(invoice.inputs)):
                await self._wait_for_deposit(invoice, index, signer)

            invoice.status = InvoiceStatus.EXECUTING
            await self._execute_transfers(invoice, signer)

            invoice.status = InvoiceStatus.DELIVERED
        except InvoiceExpiredError:
            logger.info("invoice %s expired awaiting deposit", invoice.id)
        except Exception:
            logger.exception("invoice %s failed", invoice.id)
            invoice.status = InvoiceStatus.FAILED

    async def _wait_for_deposit(self, invoice: Invoice, index: int, signer: EthSigner) -> None:
        required = invoice.inputs[index].deposit.amount
        chain = required.token.chain
        action = Action(
            protocol=None,
            description=f"wait for {required.token.symbol} deposit",
            source_chain=chain,
            destination_chain=None,
        )
        invoice.actions.append(action)
        step = action.add_step("poll_balance")
        step.start()

        async with HttpClient() as client:
            rpc = EvmRpcClient(client, self.rpc_url_for_chain(chain))
            while True:
                balance = await rpc.get_balance(signer.address)
                inp = invoice.inputs[index]
                if balance != inp.received.amount:
                    inp = replace(inp, received=replace(inp.received, amount=Amount(balance)))
                    invoice.inputs[index] = inp
                if balance >= required.amount:
                    invoice.inputs[index] = replace(inp, status=InputStatus.RECEIVED)
                    step.complete()
                    return
                if invoice.is_expired:
                    invoice.inputs[index] = replace(inp, status=InputStatus.EXPIRED)
                    step.fail("deposit timeout")
                    invoice.status = InvoiceStatus.EXPIRED
                    raise InvoiceExpiredError(invoice.id)
                await asyncio.sleep(DEPOSIT_POLL_INTERVAL)

    async def _execute_transfers(self, invoice: Invoice, signer: EthSigner) -> None:
        # the quote only routes same-chain native transfers, so everything is on the input's chain
        chain = invoice.inputs[0].deposit.address.chain
        rpc_url = self.rpc_url_for_chain(chain)
        chain_id = int(chain.caip2.split(":")[1])
        action = Action(
            protocol=None,
            description=f"send {chain.native_token} to {len(invoice.outputs)} recipients",
            source_chain=chain,
            destination_chain=None,
        )
        invoice.actions.append(action)

        for index, out in enumerate(invoice.outputs):
            step = action.add_step(f"transfer_{index}")
            step.start()

            recording = RecordingClient(step.http_exchanges)
            rpc = EvmRpcClient(recording, rpc_url)

            try:
                gas_price = int(await jsonrpc(recording, rpc_url, "eth_gasPrice"), 16)
                nonce = await rpc.get_nonce(signer.address)

                tx = signer.sign_transaction(
                    {
                        "to": out.balance.address.value,
                        "value": out.balance.amount.amount,
                        "gas": ETH_TRANSFER_GAS,
                        "maxFeePerGas": gas_price * 2,
                        "maxPriorityFeePerGas": gas_price // 10,
                        "nonce": nonce,
                        "chainId": chain_id,
                    }
                )

                await rpc.submit_tx(tx.raw_tx)
            except Exception as e:
                invoice.outputs[index] = replace(out, status=OutputStatus.FAILED, error=str(e))
                step.fail(str(e))
                raise

            submitted = Transaction(
                chain=chain,
                hash=TxHash.from_bytes(tx.tx_hash),
                timestamp=Timestamp.now(),
            )
            step.transactions.append(submitted)
            invoice.outputs[index] = replace(
                out, status=OutputStatus.SUBMITTED, transactions=(submitted,)
            )
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
