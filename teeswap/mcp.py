import abc
import base64
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Literal, TextIO

import cryptography.exceptions
from dacite import DaciteError

from .common import PKG_NAME, PKG_VERSION, TeeSwapError
from .crypto.attestation import (
    VERIFIABLE_TOOLS_NS,
    AttestationError,
    BlindExecutor,
    ProofFormat,
    Signer,
)
from .crypto.hpke import HpkeKeypair
from .response import ToolResponse
from .schema import schema_for_type
from .wire import HasFromDict, WireError, encode, parse_json
from .x402 import MCP_PAYMENT_META_KEY, PaymentPayload, PaymentTerms, ResourceInfo

logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_VERSIONS = ("2025-11-25", "2026-07-28")
PROTOCOL_VERSION_META = "io.modelcontextprotocol/protocolVersion"

SESSION_TTL_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class JsonRpcRequest(HasFromDict):
    method: str
    id: str | int
    params: dict[str, Any] = field(default_factory=dict)
    jsonrpc: str = "2.0"


@dataclass(frozen=True, slots=True)
class JsonRpcNotification(HasFromDict):
    method: str
    params: dict[str, Any] = field(default_factory=dict)
    jsonrpc: str = "2.0"


type JsonRpcMessage = JsonRpcRequest | JsonRpcNotification


def parse_message(data: dict[str, Any]) -> JsonRpcMessage:
    """JSON-RPC 2.0: with an "id" it is a request (always answered), without one a
    notification (never answered, not even with an error)."""
    if "id" in data:
        return JsonRpcRequest.from_dict(data)
    return JsonRpcNotification.from_dict(data)


# Client notifications we accept and deliberately take no action on, and why that is
# within the MCP spec. If we start sending server-initiated requests or add long-running
# tools, initialized/cancelled/progress need real handling.
IGNORED_NOTIFICATIONS: dict[str, str] = {
    "notifications/initialized": "only gates server-initiated requests; we send none",
    "notifications/cancelled": (
        "spec allows ignoring when cancellation isn't possible: stdio handles one request"
        " at a time and HTTP calls are short, so the request has completed"
    ),
    "notifications/progress": "only concerns server-initiated requests; we send none",
    "notifications/roots/list_changed": "we don't use client roots",
}


def handle_mcp_notification(notification: JsonRpcNotification) -> None:
    if notification.method not in IGNORED_NOTIFICATIONS:
        logger.debug("ignoring unknown notification %s", notification.method)


# --- MCP method params (field names are the wire names) ---


@dataclass(frozen=True, slots=True)
class VerifiableToolsMeta(HasFromDict):
    nonce: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class McpParams(HasFromDict):
    """Params any MCP method may carry. _meta is an open namespace by spec."""

    _meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        version = self._meta.get(PROTOCOL_VERSION_META)
        if version is not None and not isinstance(version, str):
            raise TypeError(f"{PROTOCOL_VERSION_META} must be a string")
        self._verifiable_tools()
        self._payment()

    @property
    def protocol_version(self) -> str | None:
        return self._meta.get(PROTOCOL_VERSION_META)

    @property
    def client_nonce(self) -> str | None:
        vt = self._verifiable_tools()
        return vt.nonce if vt is not None else None

    @property
    def payment(self) -> PaymentPayload | None:
        return self._payment()

    def _payment(self) -> PaymentPayload | None:
        raw = self._meta.get(MCP_PAYMENT_META_KEY)
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise TypeError(f"{MCP_PAYMENT_META_KEY} must be an object")
        return PaymentPayload.from_dict(raw)

    def _verifiable_tools(self) -> VerifiableToolsMeta | None:
        raw = self._meta.get(VERIFIABLE_TOOLS_NS)
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise TypeError(f"{VERIFIABLE_TOOLS_NS} must be an object")
        return VerifiableToolsMeta.from_dict(raw)


@dataclass(frozen=True, slots=True, kw_only=True)
class InitializeParams(McpParams):
    protocolVersion: str  # noqa: N815  # MCP wire field name
    capabilities: dict[str, Any]
    clientInfo: dict[str, Any]  # noqa: N815  # MCP wire field name


@dataclass(frozen=True, slots=True, kw_only=True)
class ListToolsParams(McpParams):
    cursor: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolsCallParams(McpParams):
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class BlindCallParams(McpParams):
    name: str
    inputCommitment: str  # noqa: N815  # SEP-2133 wire field name
    encryptionScheme: str  # noqa: N815  # SEP-2133 wire field name
    encryptedArguments: str  # noqa: N815  # SEP-2133 wire field name
    replyPublicKey: str | None = None  # noqa: N815  # SEP-2133 wire field name


_PARAMS_BY_METHOD: dict[str, type[McpParams]] = {
    "initialize": InitializeParams,
    "server/discover": McpParams,
    "tools/list": ListToolsParams,
    "tools/call": ToolsCallParams,
    "verifiable-tools/call": BlindCallParams,
}


@dataclass(frozen=True, slots=True)
class JsonRpcResponse:
    result: Any
    id: str | int | None = None
    jsonrpc: str = "2.0"


@dataclass(frozen=True, slots=True)
class JsonRpcError:
    code: int
    message: str
    data: Any = None


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_type: type[HasFromDict]
    output_type: type[ToolResponse]
    annotations: dict[str, bool]
    path: str | None = None
    tags: tuple[str, ...] = ()

    @property
    def input_schema(self) -> dict[str, Any]:
        return schema_for_type(self.input_type)

    @property
    def rest_path(self) -> str:
        if self.path is not None:
            return self.path
        return "/" + self.name.removeprefix(f"{PKG_NAME}_")

    @property
    def http_method(self) -> Literal["GET", "POST"]:
        if self.annotations.get("readOnly", False):
            return "GET"
        return "POST"


class ToolBase(abc.ABC):
    """What every tool has: its definition and where it may be called from."""

    @property
    @abc.abstractmethod
    def definition(self) -> ToolDefinition: ...

    @property
    def requires_session(self) -> bool:
        return False

    @property
    def mcp_visible(self) -> bool:
        return True


class Tool(ToolBase):
    @abc.abstractmethod
    async def execute(self, args: Any) -> ToolResponse: ...


class PaidTool(ToolBase):
    """A tool that may require an x402 payment.

    Not a Tool subclass: its execute takes the payment (None when the client sent
    none) and may return PaymentTerms, which a Tool never does.
    """

    @abc.abstractmethod
    async def execute(
        self, args: Any, payment: PaymentPayload | None
    ) -> ToolResponse | PaymentTerms: ...


type AnyTool = Tool | PaidTool


class ToolNotAvailableError(Exception):
    pass


class Dispatcher:
    def __init__(
        self,
        signer: Signer | None = None,
        hpke_keypair: HpkeKeypair | None = None,
    ) -> None:
        self._tools: dict[str, AnyTool] = {}
        self._signer = signer
        self._hpke_keypair = hpke_keypair
        self._blind_executor: BlindExecutor | None = (
            BlindExecutor(signer, hpke_keypair)
            if signer is not None and hpke_keypair is not None
            else None
        )

    def register(self, tool: AnyTool) -> None:
        self._tools[tool.definition.name] = tool

    @property
    def tools(self) -> dict[str, AnyTool]:
        return self._tools

    def tools_list(self, has_session: bool = True) -> list[dict[str, Any]]:
        return [
            {
                "name": t.definition.name,
                "description": t.definition.description,
                "inputSchema": t.definition.input_schema,
                "annotations": t.definition.annotations,
            }
            for t in self._tools.values()
            if t.mcp_visible and (has_session or not t.requires_session)
        ]

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        payment: PaymentPayload | None,
        has_session: bool = True,
    ) -> ToolResponse | PaymentTerms:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFoundError(f"unknown tool: {name}")
        if not tool.mcp_visible:
            raise ToolNotAvailableError(f"{name} is not available via MCP")
        if tool.requires_session and not has_session:
            raise ToolNotAvailableError(f"{name} requires a session (use stdio or stateful HTTP)")

        try:
            args = tool.definition.input_type.from_dict(arguments)
        except (DaciteError, ValueError, TypeError) as e:
            raise InvalidToolArgumentsError(f"invalid arguments for {name}: {e}") from e
        match tool:
            case PaidTool():
                return await tool.execute(args, payment)
            case Tool():
                # a payment sent to a free tool is ignored: never settled, never charged
                return await tool.execute(args)

    @property
    def signer(self) -> Signer | None:
        return self._signer

    @property
    def hpke_keypair(self) -> HpkeKeypair | None:
        return self._hpke_keypair

    @property
    def blind_executor(self) -> BlindExecutor | None:
        return self._blind_executor

    def get(self, name: str) -> AnyTool | None:
        return self._tools.get(name)


class ToolNotFoundError(Exception):
    pass


class InvalidToolArgumentsError(TeeSwapError):
    pass


# --- Session management ---


@dataclass(slots=True)
class Session:
    session_id: str
    protocol_version: str
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.last_active) > SESSION_TTL_SECONDS

    def touch(self) -> None:
        self.last_active = time.time()


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def create(self, protocol_version: str) -> Session:
        session_id = secrets.token_urlsafe(32)
        session = Session(session_id=session_id, protocol_version=protocol_version)
        self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if session.is_expired:
            del self._sessions[session_id]
            return None
        session.touch()
        return session


# --- MCP response helpers ---

_SERVER_INFO: dict[str, str] = {"name": PKG_NAME, "version": PKG_VERSION}
_CAPABILITIES: dict[str, Any] = {"tools": {"listChanged": False}}


@dataclass(frozen=True, slots=True)
class McpResult:
    body: dict[str, Any]
    session_id: str | None = None


def _ok(req_id: str | int | None, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(
    req_id: str | int | None,
    code: int,
    message: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


# --- MCP protocol handler ---


async def handle_mcp_request(
    dispatcher: Dispatcher,
    sessions: SessionManager,
    method: str,
    rpc: JsonRpcRequest,
    session_id: str | None,
) -> McpResult:
    """Always answers: anything unexpected becomes -32603 without details, logged here."""
    try:
        return await _dispatch_request(dispatcher, sessions, method, rpc, session_id)
    except Exception:
        logger.exception("internal error handling %s", method)
        return McpResult(body=_error(rpc.id, -32603, "internal error"))


async def _dispatch_request(
    dispatcher: Dispatcher,
    sessions: SessionManager,
    method: str,
    rpc: JsonRpcRequest,
    session_id: str | None,
) -> McpResult:
    params_type = _PARAMS_BY_METHOD.get(method)
    if params_type is None:
        return McpResult(body=_error(rpc.id, -32601, f"unknown method: {method}"))
    try:
        params = params_type.from_dict(rpc.params)
    except (DaciteError, ValueError, TypeError) as e:
        return McpResult(body=_error(rpc.id, -32602, f"invalid params: {e}"))

    if isinstance(params, InitializeParams):
        if params.protocolVersion not in SUPPORTED_VERSIONS:
            return McpResult(body=_unsupported_version(rpc.id, params.protocolVersion))
        session = sessions.create(params.protocolVersion)
        return McpResult(
            body=_ok(rpc.id, _init_result(dispatcher, params.protocolVersion)),
            session_id=session.session_id,
        )

    has_session = False
    if session_id is not None:
        if sessions.get(session_id) is None:
            return McpResult(body=_error(rpc.id, -32600, "invalid or expired session"))
        has_session = True

    client_version = params.protocol_version
    if client_version is not None and client_version not in SUPPORTED_VERSIONS:
        return McpResult(body=_unsupported_version(rpc.id, client_version))

    match params:
        case ToolsCallParams():
            return await _handle_tools_call(dispatcher, rpc.id, params, has_session)

        case BlindCallParams():
            return await _handle_blind_call(dispatcher, rpc.id, params)

        case ListToolsParams():
            tools = dispatcher.tools_list(has_session=has_session)
            result: dict[str, Any] = {"tools": tools}
            if not has_session:
                result["_meta"] = {"ttlMs": 60000}
            return McpResult(body=_ok(rpc.id, result))

        case _:
            return McpResult(body=_ok(rpc.id, _discover_result(dispatcher, has_session)))


def _unsupported_version(req_id: str | int | None, version: str) -> dict[str, Any]:
    return _error(
        req_id,
        -32022,
        f"unsupported protocol version: {version}",
        {"supportedVersions": list(SUPPORTED_VERSIONS)},
    )


def _init_result(dispatcher: Dispatcher, protocol_version: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "protocolVersion": protocol_version,
        "capabilities": _CAPABILITIES,
        "serverInfo": _SERVER_INFO,
    }
    if dispatcher.signer is not None:
        result["capabilities"] = {
            **_CAPABILITIES,
            "extensions": {VERIFIABLE_TOOLS_NS: _verifiable_capability(dispatcher)},
        }
    return result


def _discover_result(dispatcher: Dispatcher, has_session: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "supportedVersions": list(SUPPORTED_VERSIONS),
        "serverInfo": _SERVER_INFO,
        "tools": [
            {"name": t.definition.name, "description": t.definition.description}
            for t in dispatcher.tools.values()
            if t.mcp_visible and (has_session or not t.requires_session)
        ],
    }
    if dispatcher.signer is not None:
        result[VERIFIABLE_TOOLS_NS] = _verifiable_capability(dispatcher)
    return result


def _verifiable_capability(dispatcher: Dispatcher) -> dict[str, Any]:
    cap: dict[str, Any] = {
        "proofFormats": [f.value for f in ProofFormat],
    }
    if dispatcher.hpke_keypair is not None:
        pub_b64 = (
            base64.urlsafe_b64encode(dispatcher.hpke_keypair.public_key_bytes).decode().rstrip("=")
        )
        cap["blindExecution"] = True
        cap["blindEncryptionSchemes"] = ["hpke-v1"]
        cap["blindPublicKeys"] = {"hpke-v1": pub_b64}
    return cap


async def _handle_tools_call(
    dispatcher: Dispatcher,
    req_id: str | int | None,
    params: ToolsCallParams,
    has_session: bool = True,
) -> McpResult:
    try:
        outcome = await dispatcher.call(
            params.name, params.arguments, params.payment, has_session=has_session
        )
    except (ToolNotFoundError, ToolNotAvailableError) as e:
        return McpResult(body=_error(req_id, -32601, str(e)))
    except InvalidToolArgumentsError as e:
        return McpResult(body=_error(req_id, -32602, str(e)))
    except TeeSwapError as e:
        # a domain failure is a tool result the model can see and act on, not a protocol error
        return _attested_result(dispatcher, req_id, params, _error_content(e), {"isError": True})

    content, fields = _render_outcome(dispatcher, params.name, outcome)
    return _attested_result(dispatcher, req_id, params, content, fields)


def _error_content(error: TeeSwapError) -> list[dict[str, Any]]:
    return [{"type": "text", "text": str(error)}]


def _render_outcome(
    dispatcher: Dispatcher, name: str, outcome: ToolResponse | PaymentTerms
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The result's content, plus any other result fields (isError, structuredContent)."""
    match outcome:
        case PaymentTerms():
            # x402 MCP transport: the PaymentRequired goes in structuredContent and,
            # as JSON, in the first text content item
            required = outcome.required(
                ResourceInfo(
                    url=f"mcp://tool/{name}",
                    description=dispatcher.tools[name].definition.description,
                    mimeType="application/json",
                )
            )
            content = [{"type": "text", "text": encode(required).decode()}]
            return content, {"isError": True, "structuredContent": required}
        case ToolResponse():
            return outcome.to_mcp_content(), {}


def _attested_result(
    dispatcher: Dispatcher,
    req_id: str | int | None,
    params: ToolsCallParams,
    content: list[dict[str, Any]],
    fields: dict[str, Any],
) -> McpResult:
    result_body: dict[str, Any] = {"content": content, **fields}

    if dispatcher.signer is not None:
        verifiable = dispatcher.signer.attest_result(
            arguments=params.arguments,
            content=content,
            client_nonce=params.client_nonce,
        )
        result_body["_meta"] = verifiable.to_meta()

    return McpResult(body=_ok(req_id, result_body))


async def _handle_blind_call(
    dispatcher: Dispatcher, req_id: str | int | None, params: BlindCallParams
) -> McpResult:
    blind = dispatcher.blind_executor
    if blind is None:
        return McpResult(body=_error(req_id, -32601, "blind execution not configured"))

    if params.encryptionScheme != "hpke-v1":
        return McpResult(
            body=_error(req_id, -32602, f"unsupported scheme: {params.encryptionScheme}")
        )

    try:
        decrypted = blind.decrypt_call(
            params.name,
            params.encryptedArguments,
            params.inputCommitment,
            params.encryptionScheme,
        )
        blind.verify_commitment(decrypted, params.inputCommitment)
    except (DaciteError, ValueError, TypeError, cryptography.exceptions.InvalidTag) as e:
        return McpResult(body=_error(req_id, -32602, f"decryption failed: {e}"))
    except AttestationError as e:
        return McpResult(body=_error(req_id, -32602, str(e)))

    try:
        outcome = await dispatcher.call(params.name, decrypted.arguments, params.payment)
        content, fields = _render_outcome(dispatcher, params.name, outcome)
    except ToolNotFoundError as e:
        return McpResult(body=_error(req_id, -32601, str(e)))
    except InvalidToolArgumentsError as e:
        return McpResult(body=_error(req_id, -32602, str(e)))
    except TeeSwapError as e:
        content, fields = _error_content(e), {"isError": True}

    content, meta = blind.attest_and_encrypt(
        decrypted=decrypted,
        content=content,
        tool_name=params.name,
        client_input_commitment=params.inputCommitment,
        client_nonce=params.client_nonce,
        reply_public_key_b64=params.replyPublicKey,
    )

    # SEP-2133 blind replies encrypt `content` only; other result fields
    # (isError, x402's structuredContent) are sent as they are
    result_body: dict[str, Any] = {"content": content, **fields, "_meta": meta}
    return McpResult(body=_ok(req_id, result_body))


# --- JSONL transport ---


async def jsonl_loop(
    dispatcher: Dispatcher,
    sessions: SessionManager,
    session: Session,
    reader: TextIO,
    writer: TextIO,
) -> None:
    def write(obj: dict[str, Any]) -> None:
        writer.write(encode(obj).decode())
        writer.write("\n")
        writer.flush()

    def write_error(req_id: Any, code: int, message: str) -> None:
        write({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})

    for raw_line in reader:
        stripped = raw_line.strip()
        if not stripped:
            continue

        try:
            raw = parse_json(stripped)
        except WireError as e:
            write_error(None, -32700, f"parse error: {e}")
            continue

        if not isinstance(raw, dict):
            write_error(None, -32600, "invalid request: expected a JSON object")
            continue
        try:
            message = parse_message(raw)
        except (DaciteError, TypeError, ValueError) as e:
            write_error(raw.get("id"), -32600, f"invalid request: {e}")
            continue

        match message:
            case JsonRpcNotification():
                handle_mcp_notification(message)
            case JsonRpcRequest():
                result = await handle_mcp_request(
                    dispatcher=dispatcher,
                    sessions=sessions,
                    method=message.method,
                    rpc=message,
                    session_id=session.session_id,
                )
                try:
                    write(result.body)
                except WireError:
                    logger.exception("could not encode the reply to %s", message.method)
                    write_error(message.id, -32603, "internal error")
