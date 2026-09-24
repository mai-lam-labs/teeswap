from dataclasses import dataclass
from typing import override

from .execution.engine import Engine, X402SettlementError
from .execution.invoice import (
    Funding,
    Handover,
    Invoice,
    InvoiceId,
    InvoiceInput,
    InvoiceOutput,
    InvoiceRegistry,
    InvoiceStatus,
    InvoiceView,
)
from .mcp import PaidTool, Tool, ToolDefinition
from .response import DataclassResponse
from .types import AcceptResponse, InvoiceRequest, QuoteRequest, QuoteResponse, Timestamp
from .x402 import PaymentPayload, X402PaymentResult, X402PaymentSpec


@dataclass(frozen=True)
class StatusResponse(DataclassResponse):
    quote_id: str
    status: InvoiceStatus
    reason: str | None  # why the job ended, once it has
    expires_at: Timestamp
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
            description="Get a quote for a transfer or swap, funded by deposit. "
            "Returns estimated outputs, gas, and Mai's plan.",
            input_type=QuoteRequest,
            output_type=QuoteResponse,
            annotations={"readOnly": True, "openWorld": True},
            tags=("swap",),
        )

    @override
    async def execute(self, args: QuoteRequest) -> QuoteResponse:
        return await self._engine.quote(args, Funding.DEPOSIT)


class QuoteX402Tool(Tool):
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @property
    @override
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="teeswap_quote_x402",
            description="Get a quote for a transfer or swap, funded by an x402 payment. "
            "Returns estimated outputs, gas, and Mai's plan; pay with teeswap_accept_x402.",
            input_type=QuoteRequest,
            output_type=QuoteResponse,
            annotations={"readOnly": True, "openWorld": True},
            tags=("swap", "x402"),
        )

    @override
    async def execute(self, args: QuoteRequest) -> QuoteResponse:
        return await self._engine.quote(args, Funding.X402)


class AcceptTool(Tool):
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @property
    @override
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="teeswap_accept",
            description="Accept a deposit-funded quote and start the invoice. "
            "Returns deposit address and instructions.",
            input_type=InvoiceRequest,
            output_type=AcceptResponse,
            annotations={"readOnly": False, "idempotent": False, "openWorld": True},
            tags=("swap",),
        )

    @override
    async def execute(self, args: InvoiceRequest) -> AcceptResponse:
        return self._engine.accept(InvoiceId(args.quote_id))


class AcceptX402Tool(PaidTool):
    """Accept an x402-funded quote. Called without payment it says what to pay; paid,
    it settles the payment (which delivers the input) and starts the invoice."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @property
    @override
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="teeswap_accept_x402",
            description="Accept an x402-funded quote by paying for it, and start the invoice.",
            input_type=InvoiceRequest,
            output_type=AcceptResponse,
            annotations={"readOnly": False, "idempotent": False, "openWorld": True},
            tags=("swap", "x402"),
        )

    @override
    async def execute(
        self, args: InvoiceRequest, payment: PaymentPayload | None
    ) -> X402PaymentSpec | X402PaymentResult:
        invoice_id = InvoiceId(args.quote_id)
        if payment is None:
            return await self._engine.payment_spec(invoice_id, "payment required")
        try:
            return await self._engine.accept_x402(invoice_id, payment)
        except X402SettlementError as e:
            return await self._engine.payment_spec(invoice_id, str(e))


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
        return _status(self._registry.get(InvoiceId(args.quote_id)))


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


class ToolsDownTool(Tool):
    """The owner asks Mai to stop: once nothing is in flight, the job's result becomes the
    money itself (teeswap_handover) instead of its outputs."""

    def __init__(self, engine: Engine, registry: InvoiceRegistry) -> None:
        self._engine = engine
        self._registry = registry

    @property
    @override
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="teeswap_tools_down",
            description="Stop working on an invoice. Once nothing is in flight its status is "
            "tools_down, and teeswap_handover gives its owner the accounts and their keys.",
            input_type=InvoiceRequest,
            output_type=StatusResponse,
            annotations={"readOnly": False, "idempotent": True, "openWorld": True},
            tags=("swap",),
        )

    @override
    async def execute(self, args: InvoiceRequest) -> StatusResponse:
        invoice_id = InvoiceId(args.quote_id)
        self._engine.tools_down(invoice_id)
        return _status(self._registry.get(invoice_id))


class HandoverTool(Tool):
    """With the tools down: the money itself, as the accounts' keys. Blind calls only."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @property
    @override
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="teeswap_handover",
            description="With an invoice's tools down: every account it controls, with its "
            "private key and balances. Only as a blind call with an encrypted reply.",
            input_type=InvoiceRequest,
            output_type=Handover,
            annotations={"readOnly": False, "idempotent": True, "openWorld": True},
            tags=("swap",),
        )

    @property
    @override
    def blind_only(self) -> bool:
        return True

    @override
    async def execute(self, args: InvoiceRequest) -> Handover:
        return await self._engine.hand_over(InvoiceId(args.quote_id))


def _status(invoice: Invoice) -> StatusResponse:
    return StatusResponse(
        quote_id=str(invoice.id),
        status=invoice.status,
        reason=invoice.reason,
        expires_at=invoice.expires_at,
        inputs=invoice.input_states(),
        outputs=invoice.output_states(),
    )
