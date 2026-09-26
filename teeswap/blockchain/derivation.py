"""Chain keys by chain: which key type a chain uses, derived from a seed and a label, or
taken from a secret someone hands over."""

from ..common import TeeSwapError
from .chains import Chain, ChainFamily
from .evm import EthSigner
from .keys import ChainKey


class UnsupportedChainError(TeeSwapError):
    pass


class InvalidKeyError(TeeSwapError):
    pass


def derive_key(chain: Chain, seed: bytes, label: bytes) -> ChainKey:
    match chain.family:
        case ChainFamily.EVM:
            return EthSigner.derive(seed, label)
        case _:
            raise UnsupportedChainError(f"no keys for {chain.name} ({chain.family} chains)")


def key_from_secret(chain: Chain, secret: bytes) -> ChainKey:
    """The key a secret is, as `chain`'s key type."""
    match chain.family:
        case ChainFamily.EVM:
            try:
                return EthSigner(secret)
            except ValueError as e:
                raise InvalidKeyError(f"not a {chain.name} private key") from e
        case _:
            raise UnsupportedChainError(f"no keys for {chain.name} ({chain.family} chains)")
