"""Across V3 integration — cross-chain transfers via SpokePool deposit.

The TEE deposits tokens into SpokePool on the source chain. Across relayers
fill on the destination chain. One on-chain call per transfer.

Docs: https://docs.across.to
"""

from dataclasses import dataclass
from typing import override

from eth_typing import ChecksumAddress

from ..blockchain.chains import Chain
from ..protocol import OrderState, Protocol, ProtocolMeta
from ..types import ProtocolClass


@dataclass(frozen=True, slots=True)
class AcrossDeposit:
    depositor: ChecksumAddress
    recipient: ChecksumAddress
    input_token: ChecksumAddress
    output_token: ChecksumAddress
    input_amount: int
    output_amount: int
    destination_chain_id: int
    quote_timestamp: int
    fill_deadline: int
    exclusivity_deadline: int
    message: bytes


class AcrossProtocol(Protocol):
    @property
    @override
    def meta(self) -> ProtocolMeta:
        return ProtocolMeta(
            name="across",
            protocol_class=ProtocolClass.C,
            cross_chain=True,
            supported_chains=(),
        )

    @override
    async def poll(self, order_id: str, chain: Chain) -> OrderState:
        raise NotImplementedError
