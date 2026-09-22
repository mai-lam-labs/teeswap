"""Engine — drives invoices from accept through to delivery.

Each accepted invoice gets its own async task. The task polls for the deposit,
executes the transfers, and records everything on the work log.

The reaper runs alongside and expires stale quotes.
"""

import asyncio
import logging
from datetime import UTC, datetime

from .blockchain.chains import Chain
from .blockchain.evm import EthSigner, EvmRpcClient
from .blockchain.rpc import jsonrpc
from .http import HttpClient, RecordingClient
from .invoice import (
    Action,
    Invoice,
    InvoiceExpiredError,
    InvoiceId,
    InvoiceRegistry,
    InvoiceStateError,
    InvoiceStatus,
)
from .types import AcceptResponse, DepositInstruction, SecureUrl, Transaction

logger = logging.getLogger(__name__)

DEPOSIT_POLL_INTERVAL = 5.0
REAPER_INTERVAL = 60.0
ETH_TRANSFER_GAS = 21_000


class Engine:
    def __init__(
        self,
        registry: InvoiceRegistry,
        root_key: bytes,
        rpc_urls: dict[str, SecureUrl | str],
    ) -> None:
        self._registry = registry
        self._root_key = root_key
        self._rpc_urls = rpc_urls
        self._reaper_task: asyncio.Task[None] | None = None

    def rpc_url_for_chain(self, chain: Chain) -> SecureUrl | str:
        url = self._rpc_urls.get(chain.caip2)
        if url is None:
            raise ValueError(f"no RPC URL configured for {chain.name} ({chain.caip2})")
        return url

    def accept(self, invoice_id: InvoiceId) -> AcceptResponse:
        invoice = self._registry.get(invoice_id)
        signer = EthSigner.derive(self._root_key, invoice_id.encode())
        invoice.accept(signer)

        invoice.task = asyncio.create_task(self._run_invoice(invoice))

        deposit = DepositInstruction(
            address=signer.address,
            amount=invoice.request.input,
            chain=invoice.source_chain,
        )
        inp = invoice.request.input
        return AcceptResponse(
            quote_id=str(invoice.id),
            deposits=(deposit,),
            expires_at=invoice.expires_at,
            instructions=f"Send {inp.amount} {inp.token.symbol} to {signer.address} on {invoice.source_chain.name}",
        )

    async def _run_invoice(self, invoice: Invoice) -> None:
        signer = invoice.signer
        if signer is None:
            raise InvoiceStateError("invoice has no signer")

        try:
            rpc_url = self.rpc_url_for_chain(invoice.source_chain)
            await self._wait_for_deposit(invoice, signer, rpc_url)

            invoice.status = InvoiceStatus.EXECUTING
            await self._execute_transfers(invoice, signer, rpc_url)

            invoice.status = InvoiceStatus.DELIVERED
        except Exception:
            logger.exception("invoice %s failed", invoice.id)
            invoice.status = InvoiceStatus.FAILED

    async def _wait_for_deposit(
        self,
        invoice: Invoice,
        signer: EthSigner,
        rpc_url: SecureUrl | str,
    ) -> None:
        action = Action(
            protocol=None,
            description="wait for deposit",
            source_chain=invoice.source_chain,
            destination_chain=None,
        )
        invoice.actions.append(action)
        step = action.add_step("poll_balance")
        step.start()

        async with HttpClient() as client:
            rpc = EvmRpcClient(client, rpc_url)
            expected = invoice.request.input.amount
            while True:
                balance = await rpc.get_balance(signer.address)
                if balance >= expected:
                    step.complete()
                    return
                if invoice.is_expired:
                    step.fail("deposit timeout")
                    invoice.status = InvoiceStatus.EXPIRED
                    raise InvoiceExpiredError(invoice.id)
                await asyncio.sleep(DEPOSIT_POLL_INTERVAL)

    async def _execute_transfers(
        self,
        invoice: Invoice,
        signer: EthSigner,
        rpc_url: SecureUrl | str,
    ) -> None:
        action = Action(
            protocol=None,
            description=f"split {invoice.request.input.token.symbol} to {len(invoice.quote.outputs)} recipients",
            source_chain=invoice.source_chain,
            destination_chain=None,
        )
        invoice.actions.append(action)

        chain_id = int(invoice.source_chain.caip2.split(":")[1])

        for i, output in enumerate(invoice.quote.outputs):
            step = action.add_step(f"transfer_{i}")
            step.start()

            recording = RecordingClient(step.http_exchanges)
            rpc = EvmRpcClient(recording, rpc_url)

            gas_price = int(await jsonrpc(recording, rpc_url, "eth_gasPrice"), 16)
            nonce = await rpc.get_nonce(signer.address)

            tx = signer.sign_transaction(
                {
                    "to": output.recipient,
                    "value": output.amount,
                    "gas": ETH_TRANSFER_GAS,
                    "maxFeePerGas": gas_price * 2,
                    "maxPriorityFeePerGas": gas_price // 10,
                    "nonce": nonce,
                    "chainId": chain_id,
                }
            )

            await rpc.submit_tx(tx.raw_tx)
            step.transactions.append(
                Transaction(
                    chain=invoice.source_chain,
                    hash=tx.tx_hash,
                    timestamp=datetime.now(UTC),
                )
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
