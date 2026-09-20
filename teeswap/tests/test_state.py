import pytest

from teeswap.state import InvalidTransitionError, Order, OrderStatus


def _make_order() -> Order:
    return Order(order_id="ord_1", quote_id="qt_1", total_legs=2)


def test_happy_path() -> None:
    o = _make_order()
    assert o.status == OrderStatus.QUOTED

    o.transition(OrderStatus.PAID)
    o.transition(OrderStatus.EXECUTING)
    o.transition(OrderStatus.LEG_SUBMITTED, "leg 1 tx submitted")
    o.transition(OrderStatus.LEG_CONFIRMED, "leg 1 confirmed")
    o.transition(OrderStatus.LEG_SUBMITTED, "leg 2 tx submitted")
    o.transition(OrderStatus.LEG_CONFIRMED, "leg 2 confirmed")
    o.transition(OrderStatus.DELIVERED)
    o.transition(OrderStatus.COMPLETE)

    assert o.is_terminal
    assert len(o.history) == 8


def test_invalid_transition_raises() -> None:
    o = _make_order()
    with pytest.raises(InvalidTransitionError):
        o.transition(OrderStatus.COMPLETE)


def test_failure_to_refund() -> None:
    o = _make_order()
    o.transition(OrderStatus.PAID)
    o.transition(OrderStatus.EXECUTING)
    o.transition(OrderStatus.FAILED, "rpc timeout")
    o.transition(OrderStatus.REFUNDING)
    o.transition(OrderStatus.REFUNDED)
    assert o.is_terminal


def test_terminal_states_have_no_transitions() -> None:
    o = _make_order()
    o.transition(OrderStatus.PAID)
    o.transition(OrderStatus.EXECUTING)
    o.transition(OrderStatus.LEG_SUBMITTED)
    o.transition(OrderStatus.LEG_CONFIRMED)
    o.transition(OrderStatus.DELIVERED)
    o.transition(OrderStatus.COMPLETE)

    with pytest.raises(InvalidTransitionError):
        o.transition(OrderStatus.FAILED)
