"""Custody: where a job's funds are, and who controls them (see docs/EXECUTION.md).

Funds only change place through Movements, which operations report and the engine
applies. Holdings is the running result; it refuses a movement that takes more
from a place than is there, so the record can never claim funds that don't exist.
"""

import enum
from dataclasses import dataclass

from ..common import TeeSwapError
from ..types import Address, Amount, Hex32, Timestamp, Token, TokenAmount, TxHash
from ..wire import WireStruct


class LedgerError(TeeSwapError):
    pass


class Custody(enum.StrEnum):
    HELD = "held"  # at the job's own address; the job's key controls it
    IN_FLIGHT = "in_flight"  # committed to a transaction not yet final
    DELIVERED = "delivered"  # at a recipient; left custody
    CONSUMED = "consumed"  # spent: gas, fees


class MovementKind(enum.StrEnum):
    INPUT = "input"  # funds arrived from outside
    TRANSIT = "transit"  # held -> in flight: a transaction was sent
    OUTPUT = "output"  # in flight -> delivered
    GAS = "gas"  # held -> consumed


@dataclass(frozen=True, slots=True)
class Place(WireStruct):
    address: Address
    custody: Custody
    # while in flight: the handle that identifies what has the funds and can be checked
    # on-chain (a transaction hash, or an EIP-3009 authorization nonce)
    reference: Hex32 | None = None


@dataclass(frozen=True, slots=True)
class Position(WireStruct):
    place: Place
    amount: TokenAmount


@dataclass(frozen=True, slots=True)
class Movement(WireStruct):
    kind: MovementKind
    amount: TokenAmount
    # None only for an input: the funds come from outside the job
    source: Place | None
    destination: Place
    timestamp: Timestamp
    # the on-chain transaction that made this movement, when there is one
    transaction: TxHash | None = None

    def __post_init__(self) -> None:
        if (self.source is None) != (self.kind == MovementKind.INPUT):
            raise LedgerError(
                f"a {self.kind} movement {'needs' if self.source is None else 'has no'} source"
            )


class Holdings:
    def __init__(self) -> None:
        self._amounts: dict[tuple[Place, Token], int] = {}
        self._movements: list[Movement] = []

    def apply(self, movement: Movement) -> None:
        token = movement.amount.token
        amount = movement.amount.amount
        if movement.source is not None:
            key = (movement.source, token)
            have = self._amounts.get(key, 0)
            if have < amount:
                raise LedgerError(
                    f"cannot move {amount} {token.symbol} from {movement.source.custody} at "
                    f"{movement.source.address.value}: only {have} there"
                )
            if have == amount:
                del self._amounts[key]
            else:
                self._amounts[key] = have - amount
        key = (movement.destination, token)
        self._amounts[key] = self._amounts.get(key, 0) + amount
        self._movements.append(movement)

    def amount_at(self, place: Place, token: Token) -> int:
        return self._amounts.get((place, token), 0)

    def received(self, token: Token, address: Address) -> int:
        """Total that has arrived at `address` from outside, whatever happened to it since."""
        return sum(
            m.amount.amount
            for m in self._movements
            if m.kind == MovementKind.INPUT
            and m.amount.token == token
            and m.destination.address == address
        )

    @property
    def positions(self) -> tuple[Position, ...]:
        return tuple(
            Position(place=place, amount=TokenAmount(token=token, amount=Amount(amount)))
            for (place, token), amount in self._amounts.items()
        )

    @property
    def movements(self) -> tuple[Movement, ...]:
        return tuple(self._movements)
