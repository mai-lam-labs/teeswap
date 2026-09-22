"""RFC 9180 — Hybrid Public Key Encryption (HPKE).

Suite: DHKEM(X25519, HKDF-SHA256) / HKDF-SHA256 / AES-128-GCM (base mode).

Spec:   https://www.rfc-editor.org/rfc/rfc9180
Errata: https://www.rfc-editor.org/errata/rfc9180
Vector: RFC 9180 Appendix A.1.1 (test_hpke.py verifies against it)
"""

import hmac
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ENC_SIZE = 32
KEY_SIZE = 16
NONCE_SIZE = 12

# RFC 9180 suite constants: DHKEM(X25519, HKDF-SHA256) / HKDF-SHA256 / AES-128-GCM
_HPKE_VERSION = b"HPKE-v1"
_KEM_ID = b"\x00\x20"
_KDF_ID = b"\x00\x01"
_AEAD_ID = b"\x00\x01"
_KEM_SUITE_ID = b"KEM" + _KEM_ID
_HPKE_SUITE_ID = b"HPKE" + _KEM_ID + _KDF_ID + _AEAD_ID


def _i2osp(value: int, length: int) -> bytes:
    return value.to_bytes(length, "big")


def _hmac_extract(salt: bytes, ikm: bytes) -> bytes:
    if len(salt) == 0:
        salt = b"\x00" * 32
    return hmac.new(salt, ikm, "sha256").digest()


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    output = b""
    previous = b""
    counter = 1
    while len(output) < length:
        previous = hmac.new(prk, previous + info + bytes([counter]), "sha256").digest()
        output += previous
        counter += 1
    return output[:length]


def _labeled_extract(salt: bytes, label: str, ikm: bytes) -> bytes:
    labeled_ikm = _HPKE_VERSION + _HPKE_SUITE_ID + label.encode() + ikm
    return _hmac_extract(salt, labeled_ikm)


def _labeled_expand(prk: bytes, label: str, info: bytes, length: int) -> bytes:
    labeled_info = _i2osp(length, 2) + _HPKE_VERSION + _HPKE_SUITE_ID + label.encode() + info
    return _hkdf_expand(prk, labeled_info, length)


def _kem_labeled_extract(salt: bytes, label: str, ikm: bytes) -> bytes:
    labeled_ikm = _HPKE_VERSION + _KEM_SUITE_ID + label.encode() + ikm
    return _hmac_extract(salt, labeled_ikm)


def _kem_labeled_expand(prk: bytes, label: str, info: bytes, length: int) -> bytes:
    labeled_info = _i2osp(length, 2) + _HPKE_VERSION + _KEM_SUITE_ID + label.encode() + info
    return _hkdf_expand(prk, labeled_info, length)


def _extract_and_expand(dh: bytes, kem_context: bytes) -> bytes:
    eae_prk = _kem_labeled_extract(b"", "eae_prk", dh)
    return _kem_labeled_expand(eae_prk, "shared_secret", kem_context, 32)


def _key_schedule(shared_secret: bytes, info: bytes) -> tuple[bytes, bytes]:
    psk_id_hash = _labeled_extract(b"", "psk_id_hash", b"")
    info_hash = _labeled_extract(b"", "info_hash", info)
    context = b"\x00" + psk_id_hash + info_hash
    secret = _labeled_extract(shared_secret, "secret", b"")
    key = _labeled_expand(secret, "key", context, KEY_SIZE)
    base_nonce = _labeled_expand(secret, "base_nonce", context, NONCE_SIZE)
    return key, base_nonce


def _ecdh(private_bytes: bytes, peer_public_bytes: bytes) -> bytes:
    private_key = X25519PrivateKey.from_private_bytes(private_bytes)
    peer_public = X25519PublicKey.from_public_bytes(peer_public_bytes)
    return private_key.exchange(peer_public)


@dataclass(slots=True)
class HpkeKeypair:
    private_key_bytes: bytes
    public_key_bytes: bytes

    @classmethod
    def random(cls) -> HpkeKeypair:
        return cls._from_key(X25519PrivateKey.generate())

    @classmethod
    def from_seed(cls, seed: bytes) -> HpkeKeypair:
        return cls._from_key(X25519PrivateKey.from_private_bytes(seed))

    @classmethod
    def _from_key(cls, key: X25519PrivateKey) -> HpkeKeypair:
        return cls(
            private_key_bytes=key.private_bytes_raw(),
            public_key_bytes=key.public_key().public_bytes_raw(),
        )

    def open(self, info: bytes, aad: bytes, enc_and_ciphertext: bytes) -> bytes:
        enc = enc_and_ciphertext[:ENC_SIZE]
        ciphertext = enc_and_ciphertext[ENC_SIZE:]

        dh = _ecdh(self.private_key_bytes, enc)
        shared_secret = _extract_and_expand(dh, enc + self.public_key_bytes)
        key, base_nonce = _key_schedule(shared_secret, info)

        aesgcm = AESGCM(key)
        return aesgcm.decrypt(base_nonce, ciphertext, aad)


def hpke_seal(
    recipient_public_bytes: bytes,
    info: bytes,
    aad: bytes,
    plaintext: bytes,
    ephemeral_private: bytes | None = None,
    ephemeral_public: bytes | None = None,
) -> bytes:
    if ephemeral_private is not None and ephemeral_public is not None:
        enc = ephemeral_public
        dh = _ecdh(ephemeral_private, recipient_public_bytes)
    else:
        ephemeral = X25519PrivateKey.generate()
        enc = ephemeral.public_key().public_bytes_raw()
        dh = ephemeral.exchange(X25519PublicKey.from_public_bytes(recipient_public_bytes))

    shared_secret = _extract_and_expand(dh, enc + recipient_public_bytes)
    key, base_nonce = _key_schedule(shared_secret, info)

    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(base_nonce, plaintext, aad)
    return enc + ciphertext
