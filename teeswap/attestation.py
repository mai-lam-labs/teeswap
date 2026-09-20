import enum
import hashlib
import json
import os
import subprocess
from dataclasses import asdict, dataclass
from typing import Any, NotRequired, TypedDict

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from dacite import DaciteError

from .common import TeeSwapError, from_dict
from .config import AttestationConfig


class CloudProvider(enum.StrEnum):
    AWS = "aws"
    GCP = "gcp"


class AttestationError(TeeSwapError):
    pass


class VaportpmError(AttestationError):
    pass


class VaportpmParseError(AttestationError):
    pass


class CommitmentMismatchError(AttestationError):
    pass


class ProofFormat(enum.StrEnum):
    TEE_NITRO_V1 = "tee-nitro-v1"
    SNARKJS_V2 = "snarkjs-v2"
    NOIR_V1 = "noir-v1"
    RISC0_V1 = "risc0-v1"
    EZKL_V1 = "ezkl-v1"


# --- Raw JSON shape from vaportpm-attest (TypedDicts) ---


class RawEccCoords(TypedDict):
    x: str
    y: str


class RawTpmAttestation(TypedDict):
    attest_data: str
    signature: str


class RawNitroAttestation(TypedDict):
    document: str


class RawGcpAttestation(TypedDict):
    ak_cert_chain: str


class RawAttestationContainer(TypedDict):
    tpm: dict[str, RawTpmAttestation]
    nitro: NotRequired[RawNitroAttestation]
    gcp: NotRequired[RawGcpAttestation]


class RawVaportpmOutput(TypedDict):
    nonce: str
    pcrs: dict[str, dict[str, str]]
    ak_pubkeys: dict[str, RawEccCoords]
    attestation: RawAttestationContainer


# --- Typed domain objects (frozen dataclasses) ---


@dataclass(frozen=True, slots=True)
class EccPublicKeyCoords:
    x: str
    y: str


@dataclass(frozen=True, slots=True)
class TpmAttestationData:
    attest_data: str
    signature: str


@dataclass(frozen=True, slots=True)
class NitroAttestationData:
    document: str


@dataclass(frozen=True, slots=True)
class GcpAttestationData:
    ak_cert_chain: str


@dataclass(frozen=True, slots=True)
class AttestationContainer:
    tpm: dict[str, TpmAttestationData]
    nitro: NitroAttestationData | None = None
    gcp: GcpAttestationData | None = None


@dataclass(frozen=True, slots=True)
class VaportpmOutput:
    nonce: str
    pcrs: dict[str, dict[str, str]]
    ak_pubkeys: dict[str, EccPublicKeyCoords]
    attestation: AttestationContainer


# --- SEP-2133 constants ---

VERIFIABLE_TOOLS_NS = "io.github.ripple-node-lab/verifiable-tools"
HPKE_INFO_ARGS = f"{VERIFIABLE_TOOLS_NS}/hpke-v1/args".encode()
HPKE_INFO_REPLY = f"{VERIFIABLE_TOOLS_NS}/hpke-v1/reply".encode()


# --- SEP-2133 per-result proof ---


@dataclass(frozen=True, slots=True)
class VerifiableResult:
    input_commitment: str
    output_commitment: str
    nonce: str | None
    proof: str
    proof_format: ProofFormat
    tee_attestation: dict[str, Any] | None

    def to_meta(self) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "inputCommitment": self.input_commitment,
            "outputCommitment": self.output_commitment,
            "proof": self.proof,
            "proofFormat": self.proof_format.value,
        }
        if self.nonce is not None:
            meta["nonce"] = self.nonce
        if self.tee_attestation is not None:
            meta["teeAttestation"] = self.tee_attestation
        return {VERIFIABLE_TOOLS_NS: meta}


# --- Signer (boot-time key, per-request signing) ---


class Signer:
    def __init__(self, private_key_bytes: bytes | None = None) -> None:
        if private_key_bytes is not None:
            self._key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
        else:
            self._key = Ed25519PrivateKey.generate()

        self._public_bytes = self._key.public_key().public_bytes_raw()
        self._boot_attestation: VaportpmOutput | None = None

    @property
    def public_key_hex(self) -> str:
        return "0x" + self._public_bytes.hex()

    def set_boot_attestation(self, attestation: VaportpmOutput) -> None:
        self._boot_attestation = attestation

    def attest_result(
        self,
        arguments: dict[str, Any],
        content: list[dict[str, Any]],
        client_nonce: str | None = None,
        salt: bytes = b"",
    ) -> VerifiableResult:
        input_commitment = _compute_commitment(salt, arguments)
        output_commitment = _compute_commitment(b"", content)

        binding_fields: dict[str, str] = {
            "inputCommitment": input_commitment,
            "outputCommitment": output_commitment,
        }
        if client_nonce is not None:
            binding_fields["nonce"] = client_nonce

        signature = self._key.sign(_jcs(binding_fields))

        boot_dict: dict[str, Any] | None = None
        if self._boot_attestation is not None:
            boot_dict = asdict(self._boot_attestation)

        return VerifiableResult(
            input_commitment=input_commitment,
            output_commitment=output_commitment,
            nonce=client_nonce,
            proof="0x" + signature.hex(),
            proof_format=ProofFormat.TEE_NITRO_V1,
            tee_attestation=boot_dict,
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


def _compute_commitment(prefix: bytes, obj: dict[str, Any] | list[dict[str, Any]]) -> str:
    canonical = _jcs(obj)
    return "0x" + hashlib.sha256(prefix + canonical).hexdigest()


def _jcs(obj: dict[str, Any] | list[Any]) -> bytes:
    # TODO: replace with `jcs` or `canonicaljson` package (RFC 8785)
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


# --- vaportpm subprocess interface ---


def generate_attestation(
    config: AttestationConfig,
    nonce: bytes,
) -> VaportpmOutput:
    cmd = [config.vaportpm_attest_bin, nonce.hex()]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            check=True,
            text=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        raise VaportpmError(f"vaportpm-attest failed: {e}") from e
    return _parse_vaportpm_output(result.stdout)


def extend_pcr(config: AttestationConfig, pcr_index: int, data: bytes) -> None:
    cmd = [
        config.vaportpm_pcr_extend_bin,
        "pcr-extend",
        "--index",
        str(pcr_index),
        "--data",
        data.hex(),
    ]
    try:
        subprocess.run(cmd, capture_output=True, check=True, timeout=10)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        raise VaportpmError(f"PCR extend failed: {e}") from e


# --- Parser ---


def _parse_vaportpm_output(raw_json: str) -> VaportpmOutput:
    try:
        data = json.loads(raw_json)
        return from_dict(VaportpmOutput, data)
    except (json.JSONDecodeError, DaciteError, KeyError, TypeError) as e:
        raise VaportpmParseError(f"failed to parse vaportpm output: {e}") from e
