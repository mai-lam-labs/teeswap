"""TeeSwap — the central application instance.

Holds all state: identity, engine, monitors, dispatcher, tools.
Constructed from an optional config dict. Transports (HTTP, JSONL)
are separate — they receive an instance and expose it.
"""

import os
from dataclasses import dataclass

from .api import LocalApi
from .blockchain import ChainRegistry
from .blockchain.rpc import RpcMonitor
from .config import TeeSwapConfig
from .crypto.attestation import Signer
from .crypto.hpke import HpkeKeypair
from .execution.engine import Engine
from .execution.invoice import InvoiceRegistry
from .facilitator import FacilitatorMonitor
from .mcp import Dispatcher, SessionManager
from .tools import (
    AcceptTool,
    AcceptX402Tool,
    HandoverTool,
    InvoiceTool,
    QuoteTool,
    QuoteX402Tool,
    StatusTool,
    ToolsDownTool,
)


@dataclass
class KeyMaterial:
    signer: Signer
    hpke_keypair: HpkeKeypair
    evm_root_key: bytes


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
        self.keys = keys

        self.chain_registry = ChainRegistry(parsed.chains)
        self.facilitator_monitor = FacilitatorMonitor(parsed.facilitators)
        self.rpc_monitor = RpcMonitor(parsed.rpcs, self.chain_registry)
        self.invoice_registry = InvoiceRegistry(parsed.operator)

        rpc_urls = {rpc.chain: rpc.urls[0] for rpc in parsed.rpcs if rpc.urls}
        self.engine = Engine(
            registry=self.invoice_registry,
            root_key=keys.evm_root_key,
            rpc_urls=rpc_urls,
            facilitators=self.facilitator_monitor,
        )

        self.dispatcher = Dispatcher(signer=keys.signer, hpke_keypair=keys.hpke_keypair)
        self.sessions = SessionManager()

        quote_tool = QuoteTool(self.engine)
        accept_tool = AcceptTool(self.engine)
        status_tool = StatusTool(self.invoice_registry)
        invoice_tool = InvoiceTool(self.invoice_registry)
        self.dispatcher.register(quote_tool)
        self.dispatcher.register(accept_tool)
        self.dispatcher.register(status_tool)
        quote_x402_tool = QuoteX402Tool(self.engine)
        accept_x402_tool = AcceptX402Tool(self.engine)
        self.dispatcher.register(invoice_tool)
        self.dispatcher.register(quote_x402_tool)
        self.dispatcher.register(accept_x402_tool)
        tools_down_tool = ToolsDownTool(self.engine, self.invoice_registry)
        handover_tool = HandoverTool(self.engine)
        self.dispatcher.register(tools_down_tool)
        self.dispatcher.register(handover_tool)
        self.api = LocalApi(
            quote_tool,
            accept_tool,
            quote_x402_tool,
            accept_x402_tool,
            status_tool,
            invoice_tool,
            tools_down_tool,
            handover_tool,
        )

    def start_background_tasks(self) -> None:
        self.facilitator_monitor.start()
        self.rpc_monitor.start()
        self.engine.start_reaper()

    def stop_background_tasks(self) -> None:
        self.facilitator_monitor.stop()
        self.rpc_monitor.stop()
        self.engine.stop_reaper()
