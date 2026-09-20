import abc
from dataclasses import dataclass
from typing import Any

from .common import TeeSwapError
from .types import ProtocolClass


class RouteError(TeeSwapError):
    pass


class NoRouteError(RouteError):
    pass


class SlippageError(RouteError):
    pass


class PriceCapError(RouteError):
    pass


@dataclass(frozen=True, slots=True)
class ProtocolMeta:
    name: str
    protocol_class: ProtocolClass
    supported_chains: tuple[str, ...]


class Protocol(abc.ABC):
    @property
    @abc.abstractmethod
    def meta(self) -> ProtocolMeta: ...

    @abc.abstractmethod
    async def quote(self, input_token: str, output_token: str, amount: str) -> dict[str, Any]: ...

    @abc.abstractmethod
    async def execute(self, order_params: dict[str, Any]) -> dict[str, Any]: ...

    @abc.abstractmethod
    async def status(self, order_id: str) -> dict[str, Any]: ...
