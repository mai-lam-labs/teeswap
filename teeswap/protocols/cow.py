from typing import Any, override

from ..protocol import Protocol, ProtocolMeta
from ..types import ProtocolClass

COW_API_BASE = "https://api.cow.fi/mainnet/api/v1"


class CowProtocol(Protocol):
    @property
    @override
    def meta(self) -> ProtocolMeta:
        return ProtocolMeta(
            name="cow",
            protocol_class=ProtocolClass.A,
            supported_chains=("ethereum", "gnosis", "arbitrum", "base"),
        )

    @override
    async def quote(self, input_token: str, output_token: str, amount: str) -> dict[str, Any]:
        raise NotImplementedError

    @override
    async def execute(self, order_params: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @override
    async def status(self, order_id: str) -> dict[str, Any]:
        raise NotImplementedError
