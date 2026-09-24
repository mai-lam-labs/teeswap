"""LocalApi: the tools called in process."""

from typing import override

from ..execution.invoice import Handover, InvoiceView
from ..tools import (
    AcceptTool,
    AcceptX402Tool,
    HandoverTool,
    InvoiceTool,
    QuoteKeysTool,
    QuoteTool,
    QuoteX402Tool,
    StatusResponse,
    StatusTool,
    ToolsDownTool,
)
from ..types import (
    AcceptResponse,
    InvoiceRequest,
    KeysQuoteRequest,
    QuoteRequest,
    QuoteResponse,
)
from ..x402 import PaymentPayload, PaymentRequired, ResourceInfo, X402PaymentSpec
from .base import Api, PaidAccept


class LocalApi(Api):
    def __init__(
        self,
        quote_tool: QuoteTool,
        accept_tool: AcceptTool,
        quote_x402_tool: QuoteX402Tool,
        accept_x402_tool: AcceptX402Tool,
        status_tool: StatusTool,
        invoice_tool: InvoiceTool,
        tools_down_tool: ToolsDownTool,
        handover_tool: HandoverTool,
        quote_keys_tool: QuoteKeysTool,
    ) -> None:
        self._quote = quote_tool
        self._accept = accept_tool
        self._quote_x402 = quote_x402_tool
        self._accept_x402 = accept_x402_tool
        self._status = status_tool
        self._invoice = invoice_tool
        self._tools_down = tools_down_tool
        self._handover = handover_tool
        self._quote_keys = quote_keys_tool

    @override
    async def quote(self, request: QuoteRequest) -> QuoteResponse:
        return await self._quote.execute(request)

    @override
    async def accept(self, request: InvoiceRequest) -> AcceptResponse:
        return await self._accept.execute(request)

    @override
    async def quote_x402(self, request: QuoteRequest) -> QuoteResponse:
        return await self._quote_x402.execute(request)

    @override
    async def quote_keys(self, request: KeysQuoteRequest) -> QuoteResponse:
        return await self._quote_keys.execute(request)

    @override
    async def accept_x402(
        self, request: InvoiceRequest, payment: PaymentPayload | None
    ) -> PaymentRequired | PaidAccept:
        outcome = await self._accept_x402.execute(request, payment)
        if isinstance(outcome, X402PaymentSpec):
            # in process there is no transport to name the resource, so the tool names it
            definition = self._accept_x402.definition
            return outcome.required(
                ResourceInfo(
                    url=f"teeswap:tool/{definition.name}",
                    description=definition.description,
                    mimeType="application/json",
                )
            )
        if not isinstance(outcome.response, AcceptResponse):
            raise TypeError(f"accept_x402 returned {type(outcome.response).__name__}")
        return PaidAccept(accepted=outcome.response, settlement=outcome.settlement)

    @override
    async def status(self, request: InvoiceRequest) -> StatusResponse:
        return await self._status.execute(request)

    @override
    async def invoice(self, request: InvoiceRequest) -> InvoiceView:
        return await self._invoice.execute(request)

    @override
    async def tools_down(self, request: InvoiceRequest) -> StatusResponse:
        return await self._tools_down.execute(request)

    @override
    async def handover(self, request: InvoiceRequest) -> Handover:
        # in process: the reply never leaves the TEE, so no encryption is needed
        return await self._handover.execute(request)
