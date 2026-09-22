import enum
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .attestation import (
    VERIFIABLE_TOOLS_NS,
    ProofFormat,
    VerifiableResult,
    compute_commitment,
    jcs,
)


class VerifyFailure(enum.StrEnum):
    MISSING_COMMITMENT = "missingCommitment"
    INPUT_COMMITMENT_MISMATCH = "inputCommitmentMismatch"
    OUTPUT_COMMITMENT_MISMATCH = "outputCommitmentMismatch"
    NONCE_MISMATCH = "nonceMismatch"
    UNSUPPORTED_PROOF_FORMAT = "unsupportedProofFormat"
    INVALID_SIGNATURE = "invalidSignature"


@dataclass(frozen=True, slots=True)
class Verified:
    input_commitment: str
    output_commitment: str
    proof_format: ProofFormat


@dataclass(frozen=True, slots=True)
class VerificationFailed:
    reason: VerifyFailure


type VerificationOutcome = Verified | VerificationFailed


class Verifier:
    def __init__(
        self,
        public_key_bytes: bytes,
        proof_formats: tuple[ProofFormat, ...] = (ProofFormat.TEE_VAPORTPM_V1,),
    ) -> None:
        self._public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
        self._proof_formats = frozenset(proof_formats)

    def verify(
        self,
        result: VerifiableResult,
        arguments: dict[str, Any],
        content: list[dict[str, Any]],
        expected_nonce: str | None = None,
        salt: bytes = b"",
    ) -> VerificationOutcome:
        if not result.input_commitment or not result.output_commitment:
            return VerificationFailed(VerifyFailure.MISSING_COMMITMENT)

        if result.proof_format not in self._proof_formats:
            return VerificationFailed(VerifyFailure.UNSUPPORTED_PROOF_FORMAT)

        if expected_nonce is not None and result.nonce != expected_nonce:
            return VerificationFailed(VerifyFailure.NONCE_MISMATCH)

        computed_input = compute_commitment(salt, arguments)
        if computed_input != result.input_commitment:
            return VerificationFailed(VerifyFailure.INPUT_COMMITMENT_MISMATCH)

        computed_output = compute_commitment(b"", content)
        if computed_output != result.output_commitment:
            return VerificationFailed(VerifyFailure.OUTPUT_COMMITMENT_MISMATCH)

        binding_fields: dict[str, str] = {
            "inputCommitment": result.input_commitment,
            "outputCommitment": result.output_commitment,
        }
        if result.nonce is not None:
            binding_fields["nonce"] = result.nonce

        try:
            self._public_key.verify(
                bytes.fromhex(result.proof.removeprefix("0x")),
                jcs(binding_fields),
            )
        except InvalidSignature:
            return VerificationFailed(VerifyFailure.INVALID_SIGNATURE)

        return Verified(
            input_commitment=result.input_commitment,
            output_commitment=result.output_commitment,
            proof_format=result.proof_format,
        )

    def verify_meta(
        self,
        meta: dict[str, Any],
        arguments: dict[str, Any],
        content: list[dict[str, Any]],
        expected_nonce: str | None = None,
        salt: bytes = b"",
    ) -> VerificationOutcome:
        vt_meta = meta.get(VERIFIABLE_TOOLS_NS, {})
        result = VerifiableResult(
            input_commitment=vt_meta.get("inputCommitment", ""),
            output_commitment=vt_meta.get("outputCommitment", ""),
            nonce=vt_meta.get("nonce"),
            proof=vt_meta.get("proof", ""),
            proof_format=ProofFormat(vt_meta["proofFormat"]),
            tee_attestation=vt_meta.get("teeAttestation"),
        )
        return self.verify(result, arguments, content, expected_nonce, salt)
