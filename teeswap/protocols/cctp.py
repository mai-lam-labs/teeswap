"""CCTP V2 integration — cross-chain USDC via burn/attest/mint.

Three steps: burn on source (depositForBurn), poll Circle's Iris API for
attestation, mint on destination (receiveMessage). USDC only.

Docs: https://developers.circle.com/stablecoins/cctp-getting-started
"""

from dataclasses import dataclass
from typing import override

from eth_typing import ChecksumAddress

from ..blockchain.chains import Chain
from ..protocol import OrderState, Protocol, ProtocolMeta
from ..types import ProtocolClass


@dataclass(frozen=True, slots=True)
class CctpBurn:
    amount: int
    destination_domain: int
    mint_recipient: ChecksumAddress
    burn_token: ChecksumAddress


class CctpProtocol(Protocol):
    @property
    @override
    def meta(self) -> ProtocolMeta:
        return ProtocolMeta(
            name="cctp",
            protocol_class=ProtocolClass.C,
            cross_chain=True,
            supported_chains=(),
        )

    @override
    async def poll(self, order_id: str, chain: Chain) -> OrderState:
        raise NotImplementedError
