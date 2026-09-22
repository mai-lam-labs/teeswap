"""TeeSwap — the central application instance.

Holds all state: identity, engine, monitors, dispatcher, tools.
Constructed from an optional config dict. Transports (HTTP, JSONL)
are separate — they receive an instance and expose it.
"""

import os
from dataclasses import dataclass

from .blockchain import ChainRegistry
from .blockchain.rpc import RpcMonitor
from .config import FeeConfig, TeeSwapConfig
from .crypto.attestation import Signer
from .crypto.hpke import HpkeKeypair
from .engine import Engine
from .facilitator import FacilitatorMonitor
from .invoice import InvoiceRegistry
from .mcp import Dispatcher, SessionManager
from .tools import AcceptTool, QuoteTool, StatusResponse, StatusTool
from .types import AcceptRequest, AcceptResponse, QuoteRequest, QuoteResponse, StatusRequest


@dataclass
class KeyMaterial:
    signer: Signer
    hpke_keypair: HpkeKeypair
    evm_root_key: bytes


class Api:
    def __init__(
        self, quote_tool: QuoteTool, accept_tool: AcceptTool, status_tool: StatusTool
    ) -> None:
        self._quote = quote_tool
        self._accept = accept_tool
        self._status = status_tool

    async def quote(self, request: QuoteRequest) -> QuoteResponse:
        return await self._quote.execute(request)

    async def accept(self, request: AcceptRequest) -> AcceptResponse:
        return await self._accept.execute(request)

    async def status(self, request: StatusRequest) -> StatusResponse:
        return await self._status.execute(request)


class TeeSwap:
    def __init__(
        self,
        config: TeeSwapConfig | None = None,
        keys: KeyMaterial | None = None,
    ) -> None:
        parsed = config or TeeSwapConfig()

        if keys is None:
            keys = KeyMaterial(
                signer=Signer.from_env(),
                hpke_keypair=HpkeKeypair.random(),
                evm_root_key=os.urandom(32),
            )

        self.config = parsed
        self.fee_config = FeeConfig()
        self.keys = keys

        self.chain_registry = ChainRegistry(parsed.chains)
        self.facilitator_monitor = FacilitatorMonitor(parsed.facilitators)
        self.rpc_monitor = RpcMonitor(parsed.rpcs, self.chain_registry)
        self.invoice_registry = InvoiceRegistry()

        rpc_urls = {rpc.chain: rpc.urls[0] for rpc in parsed.rpcs if rpc.urls}
        self.engine = Engine(
            registry=self.invoice_registry,
            root_key=keys.evm_root_key,
            rpc_urls=rpc_urls,
        )

        self.dispatcher = Dispatcher(signer=keys.signer, hpke_keypair=keys.hpke_keypair)
        self.sessions = SessionManager()

        quote_tool = QuoteTool(self.engine, self.invoice_registry)
        accept_tool = AcceptTool(self.engine)
        status_tool = StatusTool(self.invoice_registry)
        self.dispatcher.register(quote_tool)
        self.dispatcher.register(accept_tool)
        self.dispatcher.register(status_tool)
        self.api = Api(quote_tool, accept_tool, status_tool)

    def start_background_tasks(self) -> None:
        self.facilitator_monitor.start()
        self.rpc_monitor.start()
        self.engine.start_reaper()

    def stop_background_tasks(self) -> None:
        self.facilitator_monitor.stop()
        self.rpc_monitor.stop()
        self.engine.stop_reaper()
