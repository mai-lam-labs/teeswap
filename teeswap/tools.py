from typing import Any, override

from .mcp import Dispatcher, Tool, ToolDefinition
from .response import ToolResponse
from .types import (
    ExecuteRequest,
    InvoiceDetailRequest,
    InvoiceListRequest,
    InvoicePayRequest,
    QuoteRequest,
    RefundRequest,
    RoutesFilter,
    StatusRequest,
)


class RoutesTool(Tool):
    @property
    @override
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="teeswap_routes",
            description="List supported swap routes, chains, and tokens. Free, cacheable.",
            input_type=RoutesFilter,
            annotations={"readOnly": True, "openWorld": True},
            tags=("discovery",),
        )

    @override
    async def execute(self, args: Any) -> ToolResponse:
        raise NotImplementedError


class QuoteTool(Tool):
    @property
    @override
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="teeswap_quote",
            description="Get a swap quote with route, risk profile, and pricing.",
            input_type=QuoteRequest,
            annotations={"readOnly": True, "openWorld": True},
            tags=("swap",),
        )

    @override
    async def execute(self, args: Any) -> ToolResponse:
        raise NotImplementedError


class ExecuteTool(Tool):
    @property
    @override
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="teeswap_execute",
            description="Execute a quoted swap. Requires x402 payment.",
            input_type=ExecuteRequest,
            annotations={"readOnly": False, "idempotent": False, "openWorld": True},
            tags=("swap",),
        )

    @override
    async def execute(self, args: Any) -> ToolResponse:
        raise NotImplementedError


class StatusTool(Tool):
    @property
    @override
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="teeswap_status",
            description="Check the status of a swap order.",
            input_type=StatusRequest,
            annotations={"readOnly": True, "openWorld": True},
            tags=("swap",),
        )

    @override
    async def execute(self, args: Any) -> ToolResponse:
        raise NotImplementedError


class RefundTool(Tool):
    @property
    @override
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="teeswap_refund",
            description="Initiate a refund for a failed or timed-out swap.",
            input_type=RefundRequest,
            annotations={"readOnly": False, "idempotent": True, "openWorld": True},
            tags=("swap",),
        )

    @override
    async def execute(self, args: Any) -> ToolResponse:
        raise NotImplementedError


class InvoiceListTool(Tool):
    @property
    @override
    def requires_session(self) -> bool:
        return True

    @property
    @override
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="teeswap_invoices",
            description="List all invoices for the current session.",
            input_type=InvoiceListRequest,
            annotations={"readOnly": True, "openWorld": True},
        )

    @override
    async def execute(self, args: Any) -> ToolResponse:
        raise NotImplementedError


class InvoiceDetailTool(Tool):
    @property
    @override
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="teeswap_invoice",
            description="Get invoice details and payment status.",
            input_type=InvoiceDetailRequest,
            annotations={"readOnly": True, "openWorld": True},
        )

    @override
    async def execute(self, args: Any) -> ToolResponse:
        raise NotImplementedError


class InvoicePayTool(Tool):
    @property
    @override
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="teeswap_pay",
            description="Submit x402 payment authorization for an invoice.",
            input_type=InvoicePayRequest,
            annotations={"readOnly": False, "idempotent": True, "openWorld": True},
        )

    @override
    async def execute(self, args: Any) -> ToolResponse:
        raise NotImplementedError


class InvoiceReceiptPdfTool(Tool):
    @property
    @override
    def mcp_visible(self) -> bool:
        return False

    @property
    @override
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="teeswap_receipt_pdf",
            description="Download PDF receipt for a fulfilled invoice.",
            input_type=InvoiceDetailRequest,
            annotations={"readOnly": True, "openWorld": True},
            path="/invoices/{invoice_id:str}/receipt.pdf",
        )

    @override
    async def execute(self, args: Any) -> ToolResponse:
        raise NotImplementedError


def register_all(dispatcher: Dispatcher) -> None:
    dispatcher.register(RoutesTool())
    dispatcher.register(QuoteTool())
    dispatcher.register(ExecuteTool())
    dispatcher.register(StatusTool())
    dispatcher.register(RefundTool())
    dispatcher.register(InvoiceListTool())
    dispatcher.register(InvoiceDetailTool())
    dispatcher.register(InvoicePayTool())
    dispatcher.register(InvoiceReceiptPdfTool())
