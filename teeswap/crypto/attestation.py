"""Verifiable MCP: the verifiable-tools extension (per-result attestation + blind execution).

Proposal: https://github.com/nicholasgasior/model-context-protocol/blob/sep-2133/docs/specification/draft/extensions/verifiable-tools.md
Reference impl: https://github.com/nicholasgasior/verifiable-mcp-tools
Namespace: io.github.ripple-node-lab/verifiable-tools (pre-acceptance; will
           become io.modelcontextprotocol/verifiable-tools if the SEP merges)

Commitment scheme: SHA-256 over JCS-canonicalised arguments/content (see jcs()).
Proof format: tee-vaportpm-v1 — Ed25519 signature + vaportpm boot attestation.
Blind execution: HPKE-encrypted arguments (RFC 9180, see hpke.py).
"""

import base64
import enum
import hashlib
import os
from dataclasses import asdict, dataclass, replace
from typing import Any, override

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ..common import TeeSwapError
from ..types import Hex32
from ..wire import Encodable, HasFromDict, WireStruct, encode, parse_json
from .hpke import HpkeKeypair, hpke_seal


class AttestationError(TeeSwapError):
    pass


class ProofFormat(enum.StrEnum):
    TEE_VAPORTPM_V1 = "tee-vaportpm-v1"


# --- Verifiable MCP constants ---

VERIFIABLE_TOOLS_NS = "io.github.ripple-node-lab/verifiable-tools"
HPKE_INFO_ARGS = f"{VERIFIABLE_TOOLS_NS}/hpke-v1/args".encode()
HPKE_INFO_REPLY = f"{VERIFIABLE_TOOLS_NS}/hpke-v1/reply".encode()


# --- Verifiable MCP per-result proof ---


@dataclass(frozen=True, slots=True)
class VerifiableResult(WireStruct):
    """The verifiable-tools _meta block; field names are the wire names."""

    inputCommitment: str  # noqa: N815  # Verifiable MCP wire field name
    outputCommitment: str  # noqa: N815  # Verifiable MCP wire field name
    proof: str
    proofFormat: ProofFormat  # noqa: N815  # Verifiable MCP wire field name
    nonce: str | None = None
    teeAttestation: dict[str, Any] | None = None  # noqa: N815  # Verifiable MCP wire field name
    encryptedContent: bool = False  # noqa: N815  # Verifiable MCP wire field name

    @override
    def to_wire(self) -> Encodable:
        """The Verifiable MCP block: unset optional fields are omitted, not null."""
        meta: dict[str, Encodable] = {
            "inputCommitment": self.inputCommitment,
            "outputCommitment": self.outputCommitment,
            "proof": self.proof,
            "proofFormat": self.proofFormat.value,
        }
        if self.nonce is not None:
            meta["nonce"] = self.nonce
        if self.teeAttestation is not None:
            meta["teeAttestation"] = self.teeAttestation
        if self.encryptedContent:
            meta["encryptedContent"] = True
        return meta

    def to_meta(self) -> dict[str, Any]:
        return {VERIFIABLE_TOOLS_NS: self.to_wire()}


# --- Signer (boot-time key, per-request signing) ---


class Signer:
    def __init__(self, private_key_bytes: bytes | None = None) -> None:
        if private_key_bytes is not None:
            self._key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
        else:
            self._key = Ed25519PrivateKey.generate()

        self._public_bytes = self._key.public_key().public_bytes_raw()
        self._boot_attestation: dict[str, Any] | None = None

    @property
    def public_key_hex(self) -> str:
        return "0x" + self._public_bytes.hex()

    def set_boot_attestation(self, attestation: Any) -> None:
        self._boot_attestation = asdict(attestation)

    def attest_result(
        self,
        arguments: dict[str, Any],
        content: list[dict[str, Any]],
        client_nonce: str | None = None,
        salt: bytes = b"",
    ) -> VerifiableResult:
        input_commitment = compute_commitment(salt, arguments)
        output_commitment = compute_commitment(b"", content)

        binding_fields: dict[str, str] = {
            "inputCommitment": input_commitment,
            "outputCommitment": output_commitment,
        }
        if client_nonce is not None:
            binding_fields["nonce"] = client_nonce

        signature = self._key.sign(jcs(binding_fields))

        return VerifiableResult(
            inputCommitment=input_commitment,
            outputCommitment=output_commitment,
            nonce=client_nonce,
            proof="0x" + signature.hex(),
            proofFormat=ProofFormat.TEE_VAPORTPM_V1,
            teeAttestation=self._boot_attestation,
        )

    def attest_blind_result(
        self,
        arguments: dict[str, Any],
        content: list[dict[str, Any]],
        salt: bytes,
        client_nonce: str | None = None,
    ) -> VerifiableResult:
        if len(salt) != 32:
            raise AttestationError("blind call salt must be exactly 32 bytes")
        return self.attest_result(
            arguments=arguments,
            content=content,
            client_nonce=client_nonce,
            salt=salt,
        )

    @property
    def private_key_bytes(self) -> bytes:
        return self._key.private_bytes_raw()

    @property
    def public_key_bytes(self) -> bytes:
        return self._public_bytes

    def sign_raw(self, data: bytes) -> bytes:
        return self._key.sign(data)

    @classmethod
    def from_env(cls) -> Signer:
        key_hex = os.environ.get("TEESWAP_SIGNING_KEY")
        if key_hex is not None:
            key_bytes = bytes.fromhex(key_hex)
            os.environ["TEESWAP_SIGNING_KEY"] = os.urandom(len(key_hex)).hex()
            del os.environ["TEESWAP_SIGNING_KEY"]
            return cls(private_key_bytes=key_bytes)
        return cls()


# --- Commitment computation ---


def compute_commitment(prefix: bytes, obj: dict[str, Any] | list[dict[str, Any]]) -> str:
    canonical = jcs(obj)
    return "0x" + hashlib.sha256(prefix + canonical).hexdigest()


def jcs(obj: dict[str, Any] | list[Any]) -> bytes:
    """Canonical JSON for commitments: the same bytes wire.encode sends.

    Sorted keys, compact, raw UTF-8. Not full RFC 8785 (no ES2015 number
    formatting), but amounts are strings on the wire and floats are rejected,
    so the numbers that make 8785 hard never reach it.
    """
    return encode(obj)


# --- Blind execution (HPKE + protocol layer) ---


@dataclass(frozen=True, slots=True)
class DecryptedBlindCall(HasFromDict):
    """The decrypted blind-call payload."""

    salt: Hex32
    arguments: dict[str, Any]


def build_args_aad(tool_name: str, input_commitment: str, encryption_scheme: str) -> bytes:
    return jcs(
        {
            "tool": tool_name,
            "inputCommitment": input_commitment,
            "encryptionScheme": encryption_scheme,
        }
    )


def build_reply_aad(tool_name: str, input_commitment: str, nonce: str | None) -> bytes:
    obj: dict[str, str] = {"tool": tool_name, "inputCommitment": input_commitment}
    if nonce is not None:
        obj["nonce"] = nonce
    return jcs(obj)


class BlindExecutor:
    def __init__(self, signer: Signer, keypair: HpkeKeypair) -> None:
        self.signer = signer
        self.keypair = keypair

    def decrypt_call(
        self,
        tool_name: str,
        encrypted_arguments_b64: str,
        input_commitment: str,
        encryption_scheme: str,
    ) -> DecryptedBlindCall:
        aad = build_args_aad(tool_name, input_commitment, encryption_scheme)
        raw = base64.urlsafe_b64decode(encrypted_arguments_b64 + "==")
        plaintext = self.keypair.open(HPKE_INFO_ARGS, aad, raw)
        payload = parse_json(plaintext)
        if not isinstance(payload, dict):
            raise TypeError("blind payload must be a JSON object")
        return DecryptedBlindCall.from_dict(payload)

    def verify_commitment(
        self,
        decrypted: DecryptedBlindCall,
        client_input_commitment: str,
    ) -> None:
        computed = compute_commitment(decrypted.salt.to_bytes(), decrypted.arguments)
        if computed != client_input_commitment:
            raise AttestationError("inputCommitment mismatch")

    def attest_and_encrypt(
        self,
        decrypted: DecryptedBlindCall,
        content: list[dict[str, Any]],
        tool_name: str,
        client_input_commitment: str,
        client_nonce: str | None = None,
        reply_public_key_b64: str | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        verifiable = self.signer.attest_blind_result(
            arguments=decrypted.arguments,
            content=content,
            salt=decrypted.salt.to_bytes(),
            client_nonce=client_nonce,
        )

        if reply_public_key_b64 is not None:
            reply_aad = build_reply_aad(tool_name, client_input_commitment, client_nonce)
            plaintext = jcs(content)
            reply_public_bytes = base64.urlsafe_b64decode(reply_public_key_b64 + "==")
            sealed = hpke_seal(reply_public_bytes, HPKE_INFO_REPLY, reply_aad, plaintext)
            encrypted_content = base64.urlsafe_b64encode(sealed).decode().rstrip("=")
            content = [{"type": "text", "text": encrypted_content}]
            verifiable = replace(verifiable, encryptedContent=True)

        return content, verifiable.to_meta()
