"""End-to-end round-trip tests through the real MCP dispatch path.

These exercise: JSON-RPC dispatch -> Tool ABC -> attestation -> verification,
using the production Signer, Dispatcher, and Verifier — no mocks.
"""

import asyncio
import base64
import json
import os
from dataclasses import dataclass
from typing import Any, override

from teeswap.crypto.attestation import (
    HPKE_INFO_ARGS,
    HPKE_INFO_REPLY,
    VERIFIABLE_TOOLS_NS,
    Signer,
    build_args_aad,
    build_reply_aad,
    compute_commitment,
    jcs,
)
from teeswap.crypto.hpke import HpkeKeypair, hpke_seal
from teeswap.crypto.verify import VerificationFailed, Verified, Verifier
from teeswap.mcp import (
    Dispatcher,
    JsonRpcRequest,
    SessionManager,
    Tool,
    ToolDefinition,
    handle_mcp_request,
)
from teeswap.response import JsonResponse
from teeswap.types import HasFromDict


@dataclass(frozen=True, slots=True)
class EchoInput(HasFromDict):
    a: int = 0
    b: int = 0


class EchoTool(Tool):
    @property
    @override
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="echo",
            description="Echoes input back.",
            input_type=EchoInput,
            annotations={"readOnly": True, "openWorld": True},
        )

    @override
    async def execute(self, args: EchoInput) -> JsonResponse:
        return JsonResponse({"echoed": True})


def _make_stack() -> tuple[Dispatcher, Signer, Verifier, SessionManager]:
    signer = Signer()
    keypair = HpkeKeypair.random()
    dispatcher = Dispatcher(signer=signer, hpke_keypair=keypair)
    dispatcher.register(EchoTool())
    verifier = Verifier(
        public_key_bytes=bytes.fromhex(signer.public_key_hex.removeprefix("0x")),
    )
    return dispatcher, signer, verifier, SessionManager()


def test_plain_tools_call_round_trip() -> None:
    dispatcher, _signer, verifier, sessions = _make_stack()

    arguments = {"a": 1, "b": 2}
    nonce = "0xdeadbeefcafebabe1234567890abcdef"

    rpc = JsonRpcRequest(
        method="tools/call",
        params={
            "name": "echo",
            "arguments": arguments,
            "_meta": {VERIFIABLE_TOOLS_NS: {"nonce": nonce}},
        },
        id=1,
    )

    result = asyncio.run(handle_mcp_request(dispatcher, sessions, "tools/call", rpc, None))
    body = result.body["result"]

    assert "content" in body
    assert "_meta" in body

    outcome = verifier.verify_meta(
        meta=body["_meta"],
        arguments=arguments,
        content=body["content"],
        expected_nonce=nonce,
    )
    assert isinstance(outcome, Verified)
    assert outcome.proof_format == "tee-vaportpm-v1"


def test_tampered_content_rejected() -> None:
    dispatcher, _signer, verifier, sessions = _make_stack()

    rpc = JsonRpcRequest(
        method="tools/call",
        params={"name": "echo", "arguments": {"a": 1}},
        id=2,
    )

    result = asyncio.run(handle_mcp_request(dispatcher, sessions, "tools/call", rpc, None))
    body = result.body["result"]

    outcome = verifier.verify_meta(
        meta=body["_meta"],
        arguments={"a": 1},
        content=[{"type": "text", "text": "TAMPERED"}],
    )
    assert isinstance(outcome, VerificationFailed)
    assert outcome.reason == "outputCommitmentMismatch"


def test_wrong_nonce_rejected() -> None:
    dispatcher, _signer, verifier, sessions = _make_stack()

    rpc = JsonRpcRequest(
        method="tools/call",
        params={
            "name": "echo",
            "arguments": {"a": 1},
            "_meta": {VERIFIABLE_TOOLS_NS: {"nonce": "0xaaaa"}},
        },
        id=3,
    )

    result = asyncio.run(handle_mcp_request(dispatcher, sessions, "tools/call", rpc, None))
    body = result.body["result"]

    outcome = verifier.verify_meta(
        meta=body["_meta"],
        arguments={"a": 1},
        content=body["content"],
        expected_nonce="0xbbbb",
    )
    assert isinstance(outcome, VerificationFailed)
    assert outcome.reason == "nonceMismatch"


def test_tampered_arguments_rejected() -> None:
    dispatcher, _signer, verifier, sessions = _make_stack()

    rpc = JsonRpcRequest(
        method="tools/call",
        params={"name": "echo", "arguments": {"a": 1}},
        id=4,
    )

    result = asyncio.run(handle_mcp_request(dispatcher, sessions, "tools/call", rpc, None))
    body = result.body["result"]

    outcome = verifier.verify_meta(
        meta=body["_meta"],
        arguments={"a": 999},
        content=body["content"],
    )
    assert isinstance(outcome, VerificationFailed)
    assert outcome.reason == "inputCommitmentMismatch"


def _encrypt_for_blind_call(
    dispatcher: Dispatcher,
    tool_name: str,
    arguments: dict[str, Any],
) -> tuple[str, str, bytes]:
    """Client-side: encrypt arguments for a verifiable-tools/call.

    Returns (inputCommitment, encryptedArguments_b64, salt).
    """
    assert dispatcher.hpke_keypair is not None
    salt = os.urandom(32)
    input_commitment = compute_commitment(salt, arguments)

    payload = jcs({"salt": "0x" + salt.hex(), "arguments": arguments})

    aad = build_args_aad(tool_name, input_commitment, "hpke-v1")
    sealed = hpke_seal(dispatcher.hpke_keypair.public_key_bytes, HPKE_INFO_ARGS, aad, payload)
    encrypted_b64 = base64.urlsafe_b64encode(sealed).decode().rstrip("=")

    return input_commitment, encrypted_b64, salt


def test_blind_call_round_trip() -> None:
    dispatcher, _signer, verifier, sessions = _make_stack()

    arguments = {"a": 1, "b": 2}
    nonce = "0xcafebabe12345678abcdef0123456789"

    input_commitment, encrypted_args, salt = _encrypt_for_blind_call(dispatcher, "echo", arguments)

    rpc = JsonRpcRequest(
        method="verifiable-tools/call",
        params={
            "name": "echo",
            "inputCommitment": input_commitment,
            "encryptionScheme": "hpke-v1",
            "encryptedArguments": encrypted_args,
            "_meta": {VERIFIABLE_TOOLS_NS: {"nonce": nonce}},
        },
        id=10,
    )

    result = asyncio.run(
        handle_mcp_request(dispatcher, sessions, "verifiable-tools/call", rpc, None)
    )
    body = result.body["result"]

    assert "content" in body
    assert "_meta" in body

    outcome = verifier.verify_meta(
        meta=body["_meta"],
        arguments=arguments,
        content=body["content"],
        expected_nonce=nonce,
        salt=salt,
    )
    assert isinstance(outcome, Verified)


def test_blind_call_with_encrypted_reply() -> None:
    dispatcher, _signer, verifier, sessions = _make_stack()

    arguments = {"a": 5, "b": 10}
    nonce = "0x1111222233334444aaaabbbbccccdddd"

    input_commitment, encrypted_args, salt = _encrypt_for_blind_call(dispatcher, "echo", arguments)

    reply_keypair = HpkeKeypair.random()
    reply_pub_b64 = base64.urlsafe_b64encode(reply_keypair.public_key_bytes).decode().rstrip("=")

    rpc = JsonRpcRequest(
        method="verifiable-tools/call",
        params={
            "name": "echo",
            "inputCommitment": input_commitment,
            "encryptionScheme": "hpke-v1",
            "encryptedArguments": encrypted_args,
            "replyPublicKey": reply_pub_b64,
            "_meta": {VERIFIABLE_TOOLS_NS: {"nonce": nonce}},
        },
        id=11,
    )

    result = asyncio.run(
        handle_mcp_request(dispatcher, sessions, "verifiable-tools/call", rpc, None)
    )
    body = result.body["result"]

    meta = body["_meta"][VERIFIABLE_TOOLS_NS]
    assert meta["encryptedContent"] is True

    encrypted_content_b64 = body["content"][0]["text"]
    encrypted_bytes = base64.urlsafe_b64decode(encrypted_content_b64 + "==")

    reply_aad = build_reply_aad("echo", input_commitment, nonce)
    plaintext = reply_keypair.open(
        HPKE_INFO_REPLY,
        reply_aad,
        encrypted_bytes,
    )
    decrypted_content = json.loads(plaintext)

    outcome = verifier.verify_meta(
        meta=body["_meta"],
        arguments=arguments,
        content=decrypted_content,
        expected_nonce=nonce,
        salt=salt,
    )
    assert isinstance(outcome, Verified)


def test_blind_call_rejects_commitment_mismatch() -> None:
    dispatcher, _signer, _verifier, sessions = _make_stack()

    arguments = {"a": 1}
    _input_commitment, encrypted_args, _salt = _encrypt_for_blind_call(
        dispatcher, "echo", arguments
    )

    rpc = JsonRpcRequest(
        method="verifiable-tools/call",
        params={
            "name": "echo",
            "inputCommitment": "0x" + "00" * 32,
            "encryptionScheme": "hpke-v1",
            "encryptedArguments": encrypted_args,
        },
        id=12,
    )

    result = asyncio.run(
        handle_mcp_request(dispatcher, sessions, "verifiable-tools/call", rpc, None)
    )
    assert "error" in result.body
    assert result.body["error"]["code"] == -32602
