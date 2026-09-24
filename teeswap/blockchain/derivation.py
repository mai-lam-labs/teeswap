"""Deriving chain keys: which key type a chain uses, made from a seed and a label."""

from ..common import TeeSwapError
from .chains import Chain, ChainFamily
from .evm import EthSigner
from .keys import ChainKey


class UnsupportedChainError(TeeSwapError):
    pass


def derive_key(chain: Chain, seed: bytes, label: bytes) -> ChainKey:
    match chain.family:
        case ChainFamily.EVM:
            return EthSigner.derive(seed, label)
        case _:
            raise UnsupportedChainError(f"no keys for {chain.name} ({chain.family} chains)")
