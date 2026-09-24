"""RestApi: a client for TeeSwap's REST interface (POST /teeswap/<tool>)."""

import base64
from typing import Any, override

import httpx
from litestar import MediaType

from ..common import PKG_NAME
from ..execution.invoice import Handover, InvoiceView
from ..mcp import ToolNotAvailableError
from ..response import ErrorResponse
from ..tools import StatusResponse
from ..types import AcceptResponse, InvoiceRequest, QuoteRequest, QuoteResponse
from ..wire import HasFromDict, WireStruct, decode_object, encode
from ..x402 import (
    HTTP_PAYMENT_REQUIRED_HEADER,
    HTTP_PAYMENT_RESPONSE_HEADER,
    HTTP_PAYMENT_SIGNATURE_HEADER,
    PaymentPayload,
    PaymentRequired,
    SettleResponse,
)
from .base import Api, PaidAccept


class RestApi(Api):
    def __init__(self, client: httpx.AsyncClient, prefix: str = f"/{PKG_NAME}") -> None:
        """`client` carries the base URL (or is an in-process test client)."""
        self._client = client
        self._prefix = prefix

    async def _post(
        self, tool: str, body: WireStruct, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        return await self._client.post(
            f"{self._prefix}/{tool}",
            content=encode(body),
            headers={"content-type": MediaType.JSON, **(headers or {})},
        )

    async def _call[R: HasFromDict](self, tool: str, body: WireStruct, result: type[R]) -> R:
        resp = await self._post(tool, body)
        _raise_for_error(resp)
        return result.from_dict(decode_object(resp.content))

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
        headers = {} if payment is None else {HTTP_PAYMENT_SIGNATURE_HEADER: _b64(payment)}
        resp = await self._post("accept_x402", request, headers)
        if resp.status_code == httpx.codes.PAYMENT_REQUIRED:
            return PaymentRequired.from_dict(_header_object(resp, HTTP_PAYMENT_REQUIRED_HEADER))
        _raise_for_error(resp)
        return PaidAccept(
            accepted=AcceptResponse.from_dict(decode_object(resp.content)),
            settlement=SettleResponse.from_dict(_header_object(resp, HTTP_PAYMENT_RESPONSE_HEADER)),
        )

    @override
    async def status(self, request: InvoiceRequest) -> StatusResponse:
        return await self._call("status", request, StatusResponse)

    @override
    async def invoice(self, request: InvoiceRequest) -> InvoiceView:
        return await self._call("invoice", request, InvoiceView)

    @override
    async def tools_down(self, request: InvoiceRequest) -> StatusResponse:
        return await self._call("tools_down", request, StatusResponse)

    @override
    async def handover(self, request: InvoiceRequest) -> Handover:
        raise ToolNotAvailableError(
            "the handover returns secrets: REST replies can be read on the way, use MCP blind calls"
        )


def _raise_for_error(resp: httpx.Response) -> None:
    if resp.is_error:
        raise ErrorResponse.from_dict(decode_object(resp.content)).to_exception()


def _b64(value: WireStruct) -> str:
    return base64.b64encode(encode(value)).decode("ascii")


def _header_object(resp: httpx.Response, header: str) -> dict[str, Any]:
    """An x402 header's value: base64-encoded JSON."""
    return decode_object(base64.b64decode(resp.headers[header], validate=True))
