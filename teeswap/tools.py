from dataclasses import dataclass
from typing import override

from .config import FeeConfig
from .engine import Engine
from .http import HttpClient
from .invoice import InvoiceId, InvoiceRegistry
from .mcp import Tool, ToolDefinition
from .quote import compute_quote
from .response import DataclassResponse
from .types import AcceptRequest, AcceptResponse, QuoteRequest, QuoteResponse, StatusRequest


@dataclass(frozen=True)
class StatusResponse(DataclassResponse):
    quote_id: str
    status: str
    deposit_address: str | None
    actions: list[dict[str, object]]


class QuoteTool(Tool):
    def __init__(self, engine: Engine, registry: InvoiceRegistry) -> None:
        self._engine = engine
        self._registry = registry

    @property
    @override
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="teeswap_quote",
            description="Get a quote for a transfer or swap. Returns estimated outputs, gas, and fees.",
            input_type=QuoteRequest,
            annotations={"readOnly": True, "openWorld": True},
            tags=("swap",),
        )

    @override
    async def execute(self, args: QuoteRequest) -> QuoteResponse:
        chain = args.input.token.chain
        rpc_url = self._engine.rpc_url_for_chain(chain)
        async with HttpClient() as client:
            quote = await compute_quote(client, rpc_url, args, FeeConfig())
        self._registry.create_quote(args, quote, chain)
        return quote


class AcceptTool(Tool):
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @property
    @override
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="teeswap_accept",
            description="Accept a quote and start the invoice. Returns deposit address and instructions.",
            input_type=AcceptRequest,
            annotations={"readOnly": False, "idempotent": False, "openWorld": True},
            tags=("swap",),
        )

    @override
    async def execute(self, args: AcceptRequest) -> AcceptResponse:
        return self._engine.accept(InvoiceId(args.quote_id))


class StatusTool(Tool):
    def __init__(self, registry: InvoiceRegistry) -> None:
        self._registry = registry

    @property
    @override
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="teeswap_status",
            description="Check the status of an invoice. Shows current step, work log, and balances.",
            input_type=StatusRequest,
            annotations={"readOnly": True, "openWorld": True},
            tags=("swap",),
        )

    @override
    async def execute(self, args: StatusRequest) -> StatusResponse:
        invoice = self._registry.get(InvoiceId(args.quote_id))
        return StatusResponse(
            quote_id=str(invoice.id),
            status=invoice.status.value,
            deposit_address=invoice.deposit_address,
            actions=[
                {
                    "description": action.description,
                    "protocol": action.protocol.meta.name if action.protocol else None,
                    "steps": [
                        {
                            "operation": step.operation,
                            "status": step.status.value,
                            "chain": step.chain.name if step.chain else None,
                            "transactions": [
                                {"chain": tx.chain.name, "hash": tx.hash.hex()}
                                for tx in step.transactions
                            ],
                            "error": step.error,
                        }
                        for step in action.steps
                    ],
                }
                for action in invoice.actions
            ],
        )
