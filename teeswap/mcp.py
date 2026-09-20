import abc
import base64
import secrets
import time
from dataclasses import dataclass, field
from importlib.metadata import metadata
from typing import Any, Literal

import cryptography.exceptions

from .attestation import VERIFIABLE_TOOLS_NS, Signer, _compute_commitment
from .common import from_dict
from .hpke import HpkeKeypair, build_args_aad, build_reply_aad, decrypt_arguments, encrypt_reply
from .invoice import PaymentRequirement
from .response import ToolResponse
from .schema import schema_for_type

_PKG = metadata("teeswap")
SERVER_NAME = _PKG["Name"]
SERVER_VERSION = _PKG["Version"]
MCP_PROTOCOL_VERSION = "2026-07-28"
SUPPORTED_VERSIONS = ("2025-11-25", "2026-07-28")

SESSION_TTL_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class JsonRpcRequest:
    method: str
    params: dict[str, Any]
    id: str | int | None = None
    jsonrpc: str = "2.0"


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
    input_type: type
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
        return "/" + self.name.removeprefix(f"{SERVER_NAME}_")

    @property
    def http_method(self) -> Literal["GET", "POST"]:
        if self.annotations.get("readOnly", False):
            return "GET"
        return "POST"


class Tool(abc.ABC):
    @property
    @abc.abstractmethod
    def definition(self) -> ToolDefinition: ...

    @property
    def requires_session(self) -> bool:
        return False

    @property
    def mcp_visible(self) -> bool:
        return True

    async def price(self, args: Any) -> PaymentRequirement | None:  # noqa: ARG002
        return None

    @abc.abstractmethod
    async def execute(self, args: Any) -> ToolResponse: ...


class ToolNotAvailableError(Exception):
    pass


class Dispatcher:
    def __init__(
        self,
        signer: Signer | None = None,
        hpke_keypair: HpkeKeypair | None = None,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        self._signer = signer
        self._hpke_keypair = hpke_keypair

    def register(self, tool: Tool) -> None:
        self._tools[tool.definition.name] = tool

    @property
    def tools(self) -> dict[str, Tool]:
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
        self, name: str, arguments: dict[str, Any], has_session: bool = True
    ) -> ToolResponse:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFoundError(f"unknown tool: {name}")
        if not tool.mcp_visible:
            raise ToolNotAvailableError(f"{name} is not available via MCP")
        if tool.requires_session and not has_session:
            raise ToolNotAvailableError(f"{name} requires a session (use stdio or stateful HTTP)")

        args = from_dict(tool.definition.input_type, arguments)
        return await tool.execute(args)

    @property
    def signer(self) -> Signer | None:
        return self._signer

    @property
    def hpke_keypair(self) -> HpkeKeypair | None:
        return self._hpke_keypair

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)


class ToolNotFoundError(Exception):
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

_SERVER_INFO: dict[str, str] = {"name": SERVER_NAME, "version": SERVER_VERSION}
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
    has_session = False

    if method == "initialize":
        protocol_version = rpc.params.get("protocolVersion", MCP_PROTOCOL_VERSION)
        if protocol_version not in SUPPORTED_VERSIONS:
            return McpResult(
                body=_error(
                    rpc.id,
                    -32022,
                    f"unsupported protocol version: {protocol_version}",
                    {"supportedVersions": list(SUPPORTED_VERSIONS)},
                )
            )
        session = sessions.create(protocol_version)
        return McpResult(
            body=_ok(rpc.id, _init_result(dispatcher)),
            session_id=session.session_id,
        )

    if session_id is not None:
        session = sessions.get(session_id)
        if session is None:
            return McpResult(body=_error(rpc.id, -32600, "invalid or expired session"))
        has_session = True

    meta = rpc.params.get("_meta", {})
    client_version = meta.get("io.modelcontextprotocol/protocolVersion")
    if client_version is not None and client_version not in SUPPORTED_VERSIONS:
        return McpResult(
            body=_error(
                rpc.id,
                -32022,
                f"unsupported protocol version: {client_version}",
                {"supportedVersions": list(SUPPORTED_VERSIONS)},
            )
        )

    match method:
        case "server/discover":
            return McpResult(body=_ok(rpc.id, _discover_result(dispatcher, has_session)))

        case "tools/list":
            tools = dispatcher.tools_list(has_session=has_session)
            result: dict[str, Any] = {"tools": tools}
            if not has_session:
                result["_meta"] = {"ttlMs": 60000}
            return McpResult(body=_ok(rpc.id, result))

        case "tools/call":
            return await _handle_tools_call(dispatcher, rpc, has_session)

        case "verifiable-tools/call":
            return await _handle_blind_call(dispatcher, rpc)

        case _:
            return McpResult(body=_error(rpc.id, -32601, f"unknown method: {method}"))


def _init_result(dispatcher: Dispatcher) -> dict[str, Any]:
    result: dict[str, Any] = {
        "protocolVersion": MCP_PROTOCOL_VERSION,
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
        "proofFormats": ["tee-nitro-v1"],
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
    dispatcher: Dispatcher, rpc: JsonRpcRequest, has_session: bool = True
) -> McpResult:
    name = rpc.params.get("name", "")
    arguments = rpc.params.get("arguments", {})

    try:
        tool_result = await dispatcher.call(name, arguments, has_session=has_session)
    except ToolNotFoundError as e:
        return McpResult(body=_error(rpc.id, -32601, str(e)))
    except ToolNotAvailableError as e:
        return McpResult(body=_error(rpc.id, -32601, str(e)))

    content = tool_result.to_mcp_content()
    result_body: dict[str, Any] = {"content": content}

    if dispatcher.signer is not None:
        client_nonce = _extract_client_nonce(rpc)
        verifiable = dispatcher.signer.attest_result(
            arguments=arguments,
            content=content,
            client_nonce=client_nonce,
        )
        result_body["_meta"] = verifiable.to_meta()

    return McpResult(body=_ok(rpc.id, result_body))


async def _handle_blind_call(dispatcher: Dispatcher, rpc: JsonRpcRequest) -> McpResult:
    if dispatcher.signer is None:
        return McpResult(body=_error(rpc.id, -32601, "verifiable-tools not enabled"))
    if dispatcher.hpke_keypair is None:
        return McpResult(body=_error(rpc.id, -32601, "blind execution not configured"))

    name = rpc.params.get("name", "")
    client_input_commitment = rpc.params.get("inputCommitment", "")
    encryption_scheme = rpc.params.get("encryptionScheme", "")
    encrypted_arguments = rpc.params.get("encryptedArguments", "")
    client_nonce = _extract_client_nonce(rpc)
    reply_public_key = rpc.params.get("replyPublicKey")

    if encryption_scheme != "hpke-v1":
        return McpResult(body=_error(rpc.id, -32602, f"unsupported scheme: {encryption_scheme}"))

    aad = build_args_aad(name, client_input_commitment, encryption_scheme)
    try:
        decrypted = decrypt_arguments(dispatcher.hpke_keypair, encrypted_arguments, aad)
    except (ValueError, KeyError, cryptography.exceptions.InvalidTag) as e:
        return McpResult(body=_error(rpc.id, -32602, f"decryption failed: {e}"))

    if len(decrypted.salt) != 32:
        return McpResult(body=_error(rpc.id, -32602, "salt must be exactly 32 bytes"))

    computed_commitment = _compute_commitment(decrypted.salt, decrypted.arguments)
    if computed_commitment != client_input_commitment:
        return McpResult(body=_error(rpc.id, -32602, "inputCommitment mismatch"))

    try:
        tool_result = await dispatcher.call(name, decrypted.arguments)
    except ToolNotFoundError as e:
        return McpResult(body=_error(rpc.id, -32601, str(e)))

    content = tool_result.to_mcp_content()

    verifiable = dispatcher.signer.attest_blind_result(
        arguments=decrypted.arguments,
        content=content,
        salt=decrypted.salt,
        client_nonce=client_nonce,
    )

    if reply_public_key is not None:
        reply_aad = build_reply_aad(name, client_input_commitment, client_nonce)
        encrypted_content = encrypt_reply(content, reply_public_key, reply_aad)
        content = [{"type": "text", "text": encrypted_content}]
        meta = verifiable.to_meta()
        meta[VERIFIABLE_TOOLS_NS]["encryptedContent"] = True
    else:
        meta = verifiable.to_meta()

    return McpResult(body=_ok(rpc.id, {"content": content, "_meta": meta}))


def _extract_client_nonce(rpc: JsonRpcRequest) -> str | None:
    meta = rpc.params.get("_meta", {})
    vt = meta.get(VERIFIABLE_TOOLS_NS, {})
    return vt.get("nonce")
