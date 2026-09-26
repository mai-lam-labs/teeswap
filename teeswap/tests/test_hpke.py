"""HPKE conformance tests against RFC 9180 A.1.1 test vector.

These values come from the Verifiable MCP reference implementation's hpke.test.ts,
which itself uses the RFC 9180 Appendix A.1.1 vector for
DHKEM(X25519, HKDF-SHA256) / HKDF-SHA256 / AES-128-GCM base mode.

If our Python implementation produces the same key, nonce, and ciphertext
from the same inputs, our HPKE is conformant with the reference.
"""

import base64

import pytest
from cryptography.exceptions import InvalidTag

from teeswap.crypto.hpke import (
    HpkeKeypair,
    _ecdh,
    _extract_and_expand,
    _key_schedule,
    hpke_seal,
)


def _jwk_d_to_bytes(d: str) -> bytes:
    return base64.urlsafe_b64decode(d + "==")


# RFC 9180 A.1.1 / reference impl test vector keys (JWK d values)
SK_E = _jwk_d_to_bytes("UsSnWKgCzYuTbs7qMUQyeY1bry1-kjXcCEqxuc-i9zY")
PK_E = bytes.fromhex("37fda3567bdbd628e88668c3c8d7e97d1d1253b6d4ea6d44c150f741f1bf4431")
SK_R = _jwk_d_to_bytes("RhLFUCY_yK1YN13z9VeqxTHSaFCQPlWp8j8h2FNOisg")
PK_R = bytes.fromhex("3948cfe0ad1ddb695d780e59077195da6c56506b027329794ab02bca80815c4d")

INFO = b"Ode on a Grecian Urn"
AAD = b"Count-0"
PLAINTEXT = b"Beauty is truth, truth beauty"

EXPECTED_KEY = bytes.fromhex("4531685d41d65f03dc48f6b8302c05b0")
EXPECTED_NONCE = bytes.fromhex("56d890e5accaaf011cff4b7d")
EXPECTED_CIPHERTEXT = bytes.fromhex(
    "f938558b5d72f1a23810b4be2ab4f84331acc02fc97babc53a52ae8218a355a96d8770ac83d07bea87e13c512a"
)


def test_key_schedule_matches_rfc_vector() -> None:
    dh = _ecdh(SK_E, PK_R)
    shared_secret = _extract_and_expand(dh, PK_E + PK_R)
    key, base_nonce = _key_schedule(shared_secret, INFO)

    assert key == EXPECTED_KEY
    assert base_nonce == EXPECTED_NONCE


def test_seal_matches_rfc_vector() -> None:
    sealed = hpke_seal(
        PK_R,
        INFO,
        AAD,
        PLAINTEXT,
        ephemeral_private=SK_E,
        ephemeral_public=PK_E,
    )
    enc = sealed[:32]
    ciphertext = sealed[32:]

    assert enc == PK_E
    assert ciphertext == EXPECTED_CIPHERTEXT


def test_open_decrypts_rfc_vector() -> None:
    recipient = HpkeKeypair(private_key_bytes=SK_R, public_key_bytes=PK_R)
    enc_and_ciphertext = PK_E + EXPECTED_CIPHERTEXT
    plaintext = recipient.open(INFO, AAD, enc_and_ciphertext)
    assert plaintext == PLAINTEXT


def test_round_trip_with_random_keys() -> None:
    kp = HpkeKeypair.random()
    info = b"test-info"
    aad = b"test-aad"
    message = b"hello from teeswap"

    sealed = hpke_seal(kp.public_key_bytes, info, aad, message)
    decrypted = kp.open(info, aad, sealed)
    assert decrypted == message


def test_wrong_aad_fails() -> None:
    kp = HpkeKeypair.random()
    sealed = hpke_seal(kp.public_key_bytes, b"info", b"correct-aad", b"secret")
    with pytest.raises(InvalidTag):
        kp.open(b"info", b"wrong-aad", sealed)
