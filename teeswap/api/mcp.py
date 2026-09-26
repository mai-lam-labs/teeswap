"""McpApi: a client for TeeSwap's MCP interface (JSON-RPC tools/call over HTTP)."""

import base64
import itertools
import os
from dataclasses import dataclass, field
from typing import Any, override

import httpx
from litestar import MediaType

from ..common import PKG_NAME, TeeSwapError
from ..crypto.attestation import (
    HPKE_INFO_ARGS,
    HPKE_INFO_REPLY,
    VERIFIABLE_TOOLS_NS,
    build_args_aad,
    build_reply_aad,
    compute_commitment,
    jcs,
)
from ..crypto.hpke import HpkeKeypair, hpke_seal
from ..execution.invoice import Handover, InvoiceView
from ..mcp import ERROR_META_KEY, MCP_PROTOCOL_VERSION, PROTOCOL_VERSION_META
from ..response import ErrorResponse
from ..tools import StatusResponse
from ..types import (
    AcceptResponse,
    InvoiceRequest,
    KeysQuoteRequest,
    QuoteRequest,
    QuoteResponse,
)
from ..wire import HasFromDict, WireStruct, decode_object, encode, parse_json
from ..x402 import (
    MCP_PAYMENT_META_KEY,
    MCP_PAYMENT_RESPONSE_META_KEY,
    PaidResponse,
    PaymentError,
    PaymentNotSettledError,
    PaymentPayload,
    PaymentRequired,
    SettleResponse,
)
from .base import Api

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


@dataclass(frozen=True, slots=True)
class _BlindCapability(HasFromDict):
    """The server's Verifiable MCP capability: where blind calls are encrypted to."""

    proofFormats: tuple[str, ...]  # noqa: N815  # Verifiable MCP wire field name
    blindExecution: bool = False  # noqa: N815  # Verifiable MCP wire field name
    blindEncryptionSchemes: tuple[str, ...] = ()  # noqa: N815  # Verifiable MCP wire field name
    blindPublicKeys: dict[str, str] = field(default_factory=dict)  # noqa: N815  # Verifiable MCP wire field name


class McpApi(Api):
    """Stateless tools/call requests, and Verifiable MCP blind calls for tools whose results
    are secret. The attestation on each result is not verified here yet, and neither is
    the server's blind key: it is taken from server/discover as given."""

    def __init__(self, client: httpx.AsyncClient, path: str = "/mcp") -> None:
        """`client` carries the base URL (or is an in-process test client)."""
        self._client = client
        self._path = path
        self._ids = itertools.count(1)
        self._blind_key: bytes | None = None

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        message = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        resp = await self._client.post(
            self._path, content=encode(message), headers={"content-type": MediaType.JSON}
        )
        return decode_object(resp.content)

    async def _call_result(self, method: str, params: dict[str, Any]) -> _ToolResult:
        reply = _RpcReply.from_dict(await self._request(method, params))
        if reply.error is not None:
            if reply.error.data is not None:
                raise reply.error.data.to_exception()
            raise TeeSwapError(reply.error.message)
        if reply.result is None:
            raise TeeSwapError(f"{method}: a JSON-RPC reply with neither result nor error")
        return reply.result

    async def _tools_call(
        self, tool: str, arguments: WireStruct, payment: PaymentPayload | None = None
    ) -> _ToolResult:
        meta: dict[str, Any] = {PROTOCOL_VERSION_META: MCP_PROTOCOL_VERSION}
        if payment is not None:
            meta[MCP_PAYMENT_META_KEY] = payment
        params = {"name": f"{PKG_NAME}_{tool}", "arguments": arguments, "_meta": meta}
        return await self._call_result("tools/call", params)

    async def _server_blind_key(self) -> bytes:
        if self._blind_key is None:
            discovered = await self._request("server/discover", {})
            capability = _BlindCapability.from_dict(discovered["result"][VERIFIABLE_TOOLS_NS])
            key = capability.blindPublicKeys.get(BLIND_SCHEME)
            if key is None:
                raise TeeSwapError("the server offers no blind execution")
            self._blind_key = _unb64url(key)
        return self._blind_key

    async def _blind_call(self, tool: str, arguments: WireStruct) -> _ToolResult:
        """A verifiable-tools/call: arguments encrypted to the TEE, the reply to a key only
        this call holds. The operator relays both and can read neither."""
        name = f"{PKG_NAME}_{tool}"
        args = decode_object(encode(arguments))
        salt = os.urandom(32)
        commitment = compute_commitment(salt, args)
        sealed = hpke_seal(
            await self._server_blind_key(),
            HPKE_INFO_ARGS,
            build_args_aad(name, commitment, BLIND_SCHEME),
            jcs({"salt": "0x" + salt.hex(), "arguments": args}),
        )
        reply_keys = HpkeKeypair.random()
        result = await self._call_result(
            "verifiable-tools/call",
            {
                "name": name,
                "inputCommitment": commitment,
                "encryptionScheme": BLIND_SCHEME,
                "encryptedArguments": _b64url(sealed),
                "replyPublicKey": _b64url(reply_keys.public_key_bytes),
                "_meta": {PROTOCOL_VERSION_META: MCP_PROTOCOL_VERSION},
            },
        )
        (sealed_reply,) = result.content
        plaintext = reply_keys.open(
            HPKE_INFO_REPLY, build_reply_aad(name, commitment, None), _unb64url(sealed_reply.text)
        )
        content = parse_json(plaintext)
        if not isinstance(content, list):
            raise TeeSwapError(f"{name}: the decrypted reply is not a content list")
        return _ToolResult(
            content=tuple(_TextContent.from_dict(item) for item in content),
            isError=result.isError,
            structuredContent=result.structuredContent,
            _meta=result.meta,
        )

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
    async def quote_keys(self, request: KeysQuoteRequest) -> QuoteResponse:
        return _output(await self._blind_call("quote_keys", request), QuoteResponse)

    @override
    async def payment_required(self, request: InvoiceRequest) -> PaymentRequired:
        result = await self._tools_call("accept_x402", request, None)
        required = _payment_required(result)
        if required is None:
            _raise_for_error(result)
            raise PaymentError("teeswap_accept_x402 answered without being paid")
        return required

    @override
    async def accept_x402(
        self, request: InvoiceRequest, payment: PaymentPayload
    ) -> PaidResponse[AcceptResponse]:
        result = await self._tools_call("accept_x402", request, payment)
        required = _payment_required(result)
        if required is not None:
            raise PaymentNotSettledError(required.error)
        accepted = _output(result, AcceptResponse)
        settlement = SettleResponse.from_dict(result.meta[MCP_PAYMENT_RESPONSE_META_KEY])
        return PaidResponse(response=accepted, settlement=settlement)

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
        return _output(await self._blind_call("handover", request), Handover)


def _payment_required(result: _ToolResult) -> PaymentRequired | None:
    """x402 MCP transport: payment required is an isError result carrying it."""
    if result.isError and result.structuredContent is not None:
        return PaymentRequired.from_dict(result.structuredContent)
    return None


def _raise_for_error(result: _ToolResult) -> None:
    if result.isError:
        error = result.meta.get(ERROR_META_KEY)
        if error is None:
            raise TeeSwapError(" ".join(c.text for c in result.content))
        raise ErrorResponse.from_dict(error).to_exception()


def _output[R: HasFromDict](result: _ToolResult, output: type[R]) -> R:
    """A tool result's value: its text content, or the error it reports, raised."""
    _raise_for_error(result)
    (content,) = result.content
    return output.from_dict(decode_object(content.text))


BLIND_SCHEME = "hpke-v1"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
