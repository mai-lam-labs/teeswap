import abc
import enum
from dataclasses import dataclass

from .blockchain.chains import Chain
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


class OrderStatus(enum.StrEnum):
    PENDING = "pending"
    OPEN = "open"
    FILLING = "filling"
    FULFILLED = "fulfilled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ProtocolMeta:
    name: str
    protocol_class: ProtocolClass
    cross_chain: bool
    supported_chains: tuple[Chain, ...]


@dataclass(frozen=True, slots=True)
class OrderState:
    order_id: str
    status: OrderStatus
    executed_sell: int | None = None
    executed_buy: int | None = None
    tx_hash: str | None = None


class Protocol(abc.ABC):
    @property
    @abc.abstractmethod
    def meta(self) -> ProtocolMeta: ...

    @abc.abstractmethod
    async def poll(self, order_id: str, chain: Chain) -> OrderState: ...
