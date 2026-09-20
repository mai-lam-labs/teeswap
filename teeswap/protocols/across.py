from typing import Any, override

from ..protocol import Protocol, ProtocolMeta
from ..types import ProtocolClass


class AcrossProtocol(Protocol):
    @property
    @override
    def meta(self) -> ProtocolMeta:
        return ProtocolMeta(
            name="across",
            protocol_class=ProtocolClass.C,
            supported_chains=(
                "ethereum",
                "arbitrum",
                "optimism",
                "base",
                "polygon",
                "linea",
                "scroll",
            ),
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
