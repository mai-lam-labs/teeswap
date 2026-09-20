"""Known-answer tests for SEP-2133 commitment construction.

These vectors are derived directly from the spec definitions:
- JCS = RFC 8785 JSON Canonicalization (sorted keys, no whitespace)
- inputCommitment = "0x" + hex(SHA-256(salt || JCS(arguments)))
- outputCommitment = "0x" + hex(SHA-256(JCS(content)))
- salt is empty for plain tools/call, 32 bytes for blind calls

The expected values are computed independently from the spec text,
not from our implementation or the reference implementation.
"""

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from teeswap.attestation import Signer, _compute_commitment, _jcs


def test_jcs_sorts_keys() -> None:
    assert _jcs({"b": 2, "a": 1}) == b'{"a":1,"b":2}'


def test_jcs_array_sorts_inner_keys() -> None:
    assert _jcs([{"type": "text", "text": "3"}]) == b'[{"text":"3","type":"text"}]'


def test_jcs_nested_object() -> None:
    result = _jcs({"z": {"b": 1, "a": 2}, "a": 0})
    assert result == b'{"a":0,"z":{"a":2,"b":1}}'


def test_input_commitment_no_salt() -> None:
    commitment = _compute_commitment(b"", {"a": 1, "b": 2})
    assert commitment == "0x43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777"


def test_output_commitment() -> None:
    content = [{"type": "text", "text": "3"}]
    commitment = _compute_commitment(b"", content)
    assert commitment == "0xdf3c1a607fa724ac6ecfcad943cd9f66624ebabed781a444564d3ed953c24b4e"


def test_input_commitment_with_salt() -> None:
    salt = bytes(range(32))
    commitment = _compute_commitment(salt, {"income": 100, "debt": 20})
    assert commitment == "0x884c76820ff75d4829ee314f2ca19ada0b6ea1c6c4853a64b1301616c97e6ef9"


def test_signer_plain_call_uses_empty_salt() -> None:
    signer = Signer()
    result = signer.attest_result(
        arguments={"a": 1, "b": 2},
        content=[{"type": "text", "text": "3"}],
    )
    assert (
        result.input_commitment
        == "0x43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777"
    )
    assert (
        result.output_commitment
        == "0xdf3c1a607fa724ac6ecfcad943cd9f66624ebabed781a444564d3ed953c24b4e"
    )


def test_signer_blind_call_uses_salt() -> None:
    signer = Signer()
    salt = bytes(range(32))
    result = signer.attest_blind_result(
        arguments={"income": 100, "debt": 20},
        content=[{"type": "text", "text": "approved"}],
        salt=salt,
    )
    assert (
        result.input_commitment
        == "0x884c76820ff75d4829ee314f2ca19ada0b6ea1c6c4853a64b1301616c97e6ef9"
    )


def test_signer_echoes_client_nonce() -> None:
    signer = Signer()
    nonce = "0x5f1c3a9e7b2d4c6f8a1e0d3b5c7f9a2e"
    result = signer.attest_result(
        arguments={"a": 1, "b": 2},
        content=[{"type": "text", "text": "3"}],
        client_nonce=nonce,
    )
    assert result.nonce == nonce


def test_signer_omits_nonce_when_not_provided() -> None:
    signer = Signer()
    result = signer.attest_result(
        arguments={"a": 1},
        content=[{"type": "text", "text": "1"}],
    )
    assert result.nonce is None
    meta = result.to_meta()
    assert "nonce" not in meta["io.github.ripple-node-lab/verifiable-tools"]


def test_proof_is_verifiable() -> None:
    signer = Signer()
    result = signer.attest_result(
        arguments={"a": 1, "b": 2},
        content=[{"type": "text", "text": "3"}],
        client_nonce="0xdeadbeef",
    )

    pub = Ed25519PublicKey.from_public_bytes(
        bytes.fromhex(signer.public_key_hex.removeprefix("0x"))
    )
    binding = _jcs(
        {
            "inputCommitment": result.input_commitment,
            "outputCommitment": result.output_commitment,
            "nonce": result.nonce,
        }
    )
    pub.verify(bytes.fromhex(result.proof.removeprefix("0x")), binding)
