"""Chain registry — definitions of supported blockchain networks.

Each chain has a CAIP-2 identifier, human-friendly names, and metadata about
its native token. The operator configures which chains are active via
TeeSwapConfig; defaults are provided.
"""

import enum
from dataclasses import dataclass


class ChainFamily(enum.StrEnum):
    EVM = "evm"
    SVM = "svm"
    STELLAR = "stellar"
    ALGORAND = "algorand"
    NEAR = "near"
    SUI = "sui"
    XRPL = "xrpl"
    TRON = "tron"
    APTOS = "aptos"


@dataclass(frozen=True, slots=True)
class Chain:
    caip2: str
    family: ChainFamily
    name: str
    short_name: str
    native_token: str
    native_decimals: int
    testnet: bool = False


class ChainRegistry:
    def __init__(self, chains: tuple[Chain, ...]) -> None:
        self._by_caip2: dict[str, Chain] = {}
        self._aliases: dict[str, str] = {}
        for chain in chains:
            self._by_caip2[chain.caip2] = chain
            self._aliases[chain.name.lower()] = chain.caip2
            self._aliases[chain.short_name.lower()] = chain.caip2

    def lookup(self, identifier: str) -> Chain | None:
        if identifier in self._by_caip2:
            return self._by_caip2[identifier]
        caip2 = self._aliases.get(identifier.lower())
        if caip2 is not None:
            return self._by_caip2[caip2]
        return None

    def all(self) -> list[Chain]:
        return list(self._by_caip2.values())

    def mainnets(self) -> list[Chain]:
        return [c for c in self._by_caip2.values() if not c.testnet]
