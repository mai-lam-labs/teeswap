"""vaportpm — TPM attestation and PCR-bound key derivation interface.

Wraps the vaportpm-attest binary (from lockboot) which provides:
- TPM 2.0 attestation quotes with cloud-specific extensions (AWS Nitro, GCP)
- PCR-bound deterministic key derivation

Source: https://github.com/aspect-build/lockboot (vaportpm crate)
"""

import hashlib
import json
import subprocess
from dataclasses import dataclass
from typing import NotRequired, TypedDict

from dacite import DaciteError

from .attestation import AttestationError, Signer
from .common import from_dict


class VaportpmError(AttestationError):
    pass


class VaportpmParseError(AttestationError):
    pass


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


# --- Boot attestation (nonce-binding) ---


@dataclass(frozen=True, slots=True)
class BootAttestation:
    signer_public_key: bytes
    hpke_public_key: bytes
    timestamp: int
    liveness_signature: bytes
    nonce: bytes
    attestation: VaportpmOutput


def build_attestation_nonce(
    signer: Signer,
    hpke_public_key: bytes,
    timestamp: int,
) -> bytes:
    timestamp_bytes = str(timestamp).encode()
    liveness_sig = signer.sign_raw(timestamp_bytes)
    return hashlib.sha256(
        signer.public_key_bytes + hpke_public_key + timestamp_bytes + liveness_sig
    ).digest()


def attest_boot(
    signer: Signer,
    hpke_public_key: bytes,
    timestamp: int,
) -> BootAttestation:
    timestamp_bytes = str(timestamp).encode()
    liveness_sig = signer.sign_raw(timestamp_bytes)
    nonce = hashlib.sha256(
        signer.public_key_bytes + hpke_public_key + timestamp_bytes + liveness_sig
    ).digest()

    attestation = generate_attestation(nonce=nonce)
    signer.set_boot_attestation(attestation)

    return BootAttestation(
        signer_public_key=signer.public_key_bytes,
        hpke_public_key=hpke_public_key,
        timestamp=timestamp,
        liveness_signature=liveness_sig,
        nonce=nonce,
        attestation=attestation,
    )


# --- vaportpm subprocess interface ---


_VAPORTPM_BIN = "vaportpm-attest"


def generate_attestation(nonce: bytes) -> VaportpmOutput:
    cmd = [_VAPORTPM_BIN, "attest", nonce.hex()]
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


def derive_pcr_bound(label: str, length: int) -> bytes:
    cmd = [_VAPORTPM_BIN, "derive", label, str(length)]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            check=True,
            text=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        raise VaportpmError(f"vaportpm-attest derive failed: {e}") from e
    return bytes.fromhex(result.stdout.strip())


def _parse_vaportpm_output(raw_json: str) -> VaportpmOutput:
    try:
        data = json.loads(raw_json)
        return from_dict(VaportpmOutput, data)
    except (json.JSONDecodeError, DaciteError, KeyError, TypeError) as e:
        raise VaportpmParseError(f"failed to parse vaportpm output: {e}") from e
