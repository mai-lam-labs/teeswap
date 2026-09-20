import enum
import time
from dataclasses import dataclass, field

from .common import TeeSwapError


class OrderError(TeeSwapError):
    pass


class InvalidTransitionError(OrderError):
    pass


class OrderExpiredError(OrderError):
    pass


class OrderNotFoundError(OrderError):
    pass


class OrderStatus(enum.StrEnum):
    QUOTED = "quoted"
    PAID = "paid"
    EXECUTING = "executing"
    LEG_SUBMITTED = "leg_submitted"
    LEG_CONFIRMED = "leg_confirmed"
    DELIVERED = "delivered"
    COMPLETE = "complete"
    FAILED = "failed"
    REFUNDING = "refunding"
    REFUNDED = "refunded"


TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.QUOTED: frozenset({OrderStatus.PAID, OrderStatus.FAILED}),
    OrderStatus.PAID: frozenset({OrderStatus.EXECUTING, OrderStatus.REFUNDING}),
    OrderStatus.EXECUTING: frozenset(
        {
            OrderStatus.LEG_SUBMITTED,
            OrderStatus.FAILED,
        }
    ),
    OrderStatus.LEG_SUBMITTED: frozenset(
        {
            OrderStatus.LEG_CONFIRMED,
            OrderStatus.FAILED,
        }
    ),
    OrderStatus.LEG_CONFIRMED: frozenset(
        {
            OrderStatus.LEG_SUBMITTED,
            OrderStatus.DELIVERED,
            OrderStatus.FAILED,
        }
    ),
    OrderStatus.DELIVERED: frozenset({OrderStatus.COMPLETE}),
    OrderStatus.COMPLETE: frozenset(),
    OrderStatus.FAILED: frozenset({OrderStatus.REFUNDING}),
    OrderStatus.REFUNDING: frozenset({OrderStatus.REFUNDED, OrderStatus.FAILED}),
    OrderStatus.REFUNDED: frozenset(),
}


@dataclass(slots=True)
class Transition:
    from_status: OrderStatus
    to_status: OrderStatus
    timestamp: float
    detail: str


@dataclass(slots=True)
class Order:
    order_id: str
    quote_id: str
    status: OrderStatus = OrderStatus.QUOTED
    current_leg: int = 0
    total_legs: int = 0
    history: list[Transition] = field(default_factory=list)

    def transition(self, to: OrderStatus, detail: str = "") -> None:
        allowed = TRANSITIONS.get(self.status, frozenset())
        if to not in allowed:
            raise InvalidTransitionError(f"{self.status.value} -> {to.value} not allowed")
        prev = self.status
        self.status = to
        self.history.append(
            Transition(
                from_status=prev,
                to_status=to,
                timestamp=time.time(),
                detail=detail,
            )
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in (OrderStatus.COMPLETE, OrderStatus.REFUNDED)
