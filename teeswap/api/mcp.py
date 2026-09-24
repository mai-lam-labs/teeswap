"""McpApi: a client for TeeSwap's MCP interface (JSON-RPC tools/call over HTTP)."""

import itertools
from dataclasses import dataclass, field
from typing import Any, override

import httpx
from litestar import MediaType

from ..common import PKG_NAME, TeeSwapError
from ..execution.invoice import InvoiceView
from ..mcp import ERROR_META_KEY, MCP_PROTOCOL_VERSION, PROTOCOL_VERSION_META
from ..response import ErrorResponse
from ..tools import StatusResponse
from ..types import AcceptResponse, InvoiceRequest, QuoteRequest, QuoteResponse
from ..wire import HasFromDict, WireStruct, decode_object, encode
from ..x402 import (
    MCP_PAYMENT_META_KEY,
    MCP_PAYMENT_RESPONSE_META_KEY,
    PaymentPayload,
    PaymentRequired,
    SettleResponse,
)
from .base import Api, PaidAccept

# --- The replies we read (field names are the wire names) ---


@dataclass(frozen=True, slots=True)
class _TextContent(HasFromDict):
    type: str
    text: str


@dataclass(frozen=True, slots=True)
class _ToolResult(HasFromDict):
    content: tuple[_TextContent, ...]
    isError: bool = False  # noqa: N815  # MCP wire field name
    structuredContent: dict[str, Any] | None = None  # noqa: N815  # MCP wire field name
    _meta: dict[str, Any] = field(default_factory=dict)

    @property
    def meta(self) -> dict[str, Any]:
        return self._meta


@dataclass(frozen=True, slots=True)
class _RpcError(HasFromDict):
    code: int
    message: str
    data: ErrorResponse | None = None


@dataclass(frozen=True, slots=True)
class _RpcReply(HasFromDict):
    jsonrpc: str
    id: int
    result: _ToolResult | None = None
    error: _RpcError | None = None


class McpApi(Api):
    """Stateless tools/call requests. The SEP-2133 attestation on each result is not
    verified here yet."""

    def __init__(self, client: httpx.AsyncClient, path: str = "/mcp") -> None:
        """`client` carries the base URL (or is an in-process test client)."""
        self._client = client
        self._path = path
        self._ids = itertools.count(1)

    async def _tools_call(
        self, tool: str, arguments: WireStruct, payment: PaymentPayload | None = None
    ) -> _ToolResult:
        meta: dict[str, Any] = {PROTOCOL_VERSION_META: MCP_PROTOCOL_VERSION}
        if payment is not None:
            meta[MCP_PAYMENT_META_KEY] = payment
        message = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": "tools/call",
            "params": {"name": f"{PKG_NAME}_{tool}", "arguments": arguments, "_meta": meta},
        }
        resp = await self._client.post(
            self._path, content=encode(message), headers={"content-type": MediaType.JSON}
        )
        reply = _RpcReply.from_dict(decode_object(resp.content))
        if reply.error is not None:
            if reply.error.data is not None:
                raise reply.error.data.to_exception()
            raise TeeSwapError(reply.error.message)
        if reply.result is None:
            raise TeeSwapError(f"{tool}: a JSON-RPC reply with neither result nor error")
        return reply.result

    async def _call[R: HasFromDict](self, tool: str, arguments: WireStruct, result: type[R]) -> R:
        return _output(await self._tools_call(tool, arguments), result)

    @override
    async def quote(self, request: QuoteRequest) -> QuoteResponse:
        return await self._call("quote", request, QuoteResponse)

    @override
    async def accept(self, request: InvoiceRequest) -> AcceptResponse:
        return await self._call("accept", request, AcceptResponse)

    @override
    async def quote_x402(self, request: QuoteRequest) -> QuoteResponse:
        return await self._call("quote_x402", request, QuoteResponse)

    @override
    async def accept_x402(
        self, request: InvoiceRequest, payment: PaymentPayload | None
    ) -> PaymentRequired | PaidAccept:
        result = await self._tools_call("accept_x402", request, payment)
        if result.isError and result.structuredContent is not None:
            # x402 MCP transport: payment required, as an isError result
            return PaymentRequired.from_dict(result.structuredContent)
        accepted = _output(result, AcceptResponse)
        settlement = SettleResponse.from_dict(result.meta[MCP_PAYMENT_RESPONSE_META_KEY])
        return PaidAccept(accepted=accepted, settlement=settlement)

    @override
    async def status(self, request: InvoiceRequest) -> StatusResponse:
        return await self._call("status", request, StatusResponse)

    @override
    async def invoice(self, request: InvoiceRequest) -> InvoiceView:
        return await self._call("invoice", request, InvoiceView)


def _output[R: HasFromDict](result: _ToolResult, output: type[R]) -> R:
    """A tool result's value: its text content, or the error it reports, raised."""
    if result.isError:
        error = result.meta.get(ERROR_META_KEY)
        if error is None:
            raise TeeSwapError(" ".join(c.text for c in result.content))
        raise ErrorResponse.from_dict(error).to_exception()
    (content,) = result.content
    return output.from_dict(decode_object(content.text))
