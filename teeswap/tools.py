from dataclasses import dataclass
from datetime import datetime
from typing import override

from .config import FeeConfig
from .engine import Engine
from .http import HttpClient
from .invoice import (
    InvoiceId,
    InvoiceInput,
    InvoiceOutput,
    InvoiceRegistry,
    InvoiceStatus,
    InvoiceView,
)
from .mcp import Tool, ToolDefinition
from .quote import compute_quote
from .response import DataclassResponse
from .types import AcceptResponse, InvoiceRequest, QuoteRequest, QuoteResponse


@dataclass(frozen=True)
class StatusResponse(DataclassResponse):
    quote_id: str
    status: InvoiceStatus
    expires_at: datetime
    inputs: tuple[InvoiceInput, ...]
    outputs: tuple[InvoiceOutput, ...]


class QuoteTool(Tool):
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @property
    @override
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="teeswap_quote",
            description="Get a quote for a transfer or swap. Returns estimated outputs, gas, and fees.",
            input_type=QuoteRequest,
            output_type=QuoteResponse,
            annotations={"readOnly": True, "openWorld": True},
            tags=("swap",),
        )

    @override
    async def execute(self, args: QuoteRequest) -> QuoteResponse:
        async with HttpClient() as client:
            quote = await compute_quote(client, self._engine.rpc_url_for_chain, args, FeeConfig())
        self._engine.create_invoice(args, quote)
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
            input_type=InvoiceRequest,
            output_type=AcceptResponse,
            annotations={"readOnly": False, "idempotent": False, "openWorld": True},
            tags=("swap",),
        )

    @override
    async def execute(self, args: InvoiceRequest) -> AcceptResponse:
        return self._engine.accept(InvoiceId(args.quote_id))


class StatusTool(Tool):
    def __init__(self, registry: InvoiceRegistry) -> None:
        self._registry = registry

    @property
    @override
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="teeswap_status",
            description="Check the status of an invoice: each input and output, and the overall status.",
            input_type=InvoiceRequest,
            output_type=StatusResponse,
            annotations={"readOnly": True, "openWorld": True},
            tags=("swap",),
        )

    @override
    async def execute(self, args: InvoiceRequest) -> StatusResponse:
        invoice = self._registry.get(InvoiceId(args.quote_id))
        return StatusResponse(
            quote_id=str(invoice.id),
            status=invoice.status,
            expires_at=invoice.expires_at,
            inputs=tuple(invoice.inputs),
            outputs=tuple(invoice.outputs),
        )


class InvoiceTool(Tool):
    def __init__(self, registry: InvoiceRegistry) -> None:
        self._registry = registry

    @property
    @override
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="teeswap_invoice",
            description="Get the full invoice: inputs, outputs, estimates, and work log.",
            input_type=InvoiceRequest,
            output_type=InvoiceView,
            annotations={"readOnly": True, "openWorld": True},
            tags=("swap",),
        )

    @override
    async def execute(self, args: InvoiceRequest) -> InvoiceView:
        return self._registry.get(InvoiceId(args.quote_id)).view()
