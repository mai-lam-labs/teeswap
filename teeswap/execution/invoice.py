"""Invoice — the append-only record of work performed for a user.

Lifecycle: quote (signer derived, inputs and outputs known, TTL) → accept
(coroutine started, deposits watched) → funded → executing → delivered.

Inputs and outputs carry their own state, set by the engine as it goes; the
Action/Step work log is the detailed record of how it got there.

One ID throughout. The quote IS the invoice before it's accepted.

See docs/INVOICING.md for the design principles.
"""

import asyncio
import enum
import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import timedelta

from ..common import TeeSwapError
from ..config import Operator
from ..facilitator import Reliability
from ..response import DataclassResponse
from ..types import (
    Address,
    Amount,
    Balance,
    Hex32,
    QuoteRequest,
    QuoteResponse,
    Timestamp,
    TokenAmount,
    Transaction,
    TxHash,
)
from ..wire import WireStruct
from .accounts import Account, Accounts
from .ledger import Custody, Holdings, Movement, MovementKind, Place, Position
from .worklog import Action, ActionView, StepId, WorkLog


class InvoiceError(TeeSwapError):
    pass


class InvoiceNotFoundError(InvoiceError):
    pass


class InvoiceExpiredError(InvoiceError):
    pass


class InvoiceStateError(InvoiceError):
    pass


class InvoiceFundingError(InvoiceStateError):
    pass


class InvoiceId(str):
    """The invoice's id, and the credential for it: whoever holds it owns the invoice and
    the funds it holds. Never log it, display it, or send it anywhere but back to its
    owner; anything else names the invoice by its `ref`."""

    @classmethod
    def generate(cls) -> InvoiceId:
        return cls("inv_" + secrets.token_hex(32))

    @property
    def ref(self) -> InvoiceRef:
        digest = hashlib.sha256(b"teeswap/invoice-ref\x00" + self.encode()).digest()
        return InvoiceRef("ref_" + digest[:16].hex())


class InvoiceRef(str):
    """An invoice's public name: one-way from its id, so it identifies the invoice to
    the operator, facilitators and logs without granting anything."""


# --- Invoice ---


class InvoiceStatus(enum.StrEnum):
    QUOTED = "quoted"
    AWAITING_DEPOSIT = "awaiting_deposit"
    EXECUTING = "executing"
    DELIVERED = "delivered"
    # Mai has stopped: instead of the outputs, the job's result is the money itself,
    # every account it controls, handed to the invoice's owner (see `reason` for why)
    TOOLS_DOWN = "tools_down"
    EXPIRED = "expired"  # a quote nobody accepted


class Funding(enum.StrEnum):
    """How the job's inputs reach its address; chosen at quote time."""

    DEPOSIT = "deposit"  # the client transfers them
    X402 = "x402"  # an x402 payment, settled by a facilitator, delivers them
    KEYS = "keys"  # they're already there: the client handed over the accounts' keys


class InputStatus(enum.StrEnum):
    AWAITING = "awaiting"
    RECEIVED = "received"
    EXPIRED = "expired"  # the quote expired before it was accepted
    NOT_RECEIVED = "not_received"  # the tools went down before it arrived


class OutputStatus(enum.StrEnum):
    PENDING = "pending"  # not sent, or a sent attempt came back
    SUBMITTED = "submitted"  # in flight
    DELIVERED = "delivered"


@dataclass(frozen=True, slots=True)
class InvoiceInput(WireStruct):
    """An input's progress, derived from the holdings."""

    deposit: Balance
    received: TokenAmount
    status: InputStatus


@dataclass(frozen=True, slots=True)
class InvoiceOutput(WireStruct):
    """An output's progress, derived from the holdings; transactions include failed attempts."""

    balance: Balance
    status: OutputStatus
    transactions: tuple[Transaction, ...]
    attempts: int  # transfers sent for it, including those that came back


@dataclass(frozen=True)
class InvoiceView(DataclassResponse):
    """The public invoice: what the invoice page shows, as data."""

    quote_id: str
    operator: Operator
    funding: Funding
    status: InvoiceStatus
    reason: str | None  # why the job ended, once it has
    created_at: Timestamp
    expires_at: Timestamp
    inputs: tuple[InvoiceInput, ...]
    outputs: tuple[InvoiceOutput, ...]
    gas: TokenAmount
    accounts: tuple[Account, ...]
    holdings: tuple[Position, ...]
    movements: tuple[Movement, ...]
    actions: tuple[ActionView, ...]


@dataclass(frozen=True, slots=True)
class ReleasedAccount(WireStruct):
    address: Address
    purpose: str
    private_key: Hex32  # the secret: holding it is holding the money
    balances: tuple[TokenAmount, ...]  # what the chain shows there as it's handed over


@dataclass(frozen=True)
class Handover(DataclassResponse):
    """What an invoice's owner gets with the tools down: every account the job controls,
    with its key and what it holds. The funds don't move: control of them does."""

    status: InvoiceStatus
    reason: str | None
    accounts: tuple[ReleasedAccount, ...]


QUOTE_TTL = timedelta(minutes=5)
DEPOSIT_TTL = timedelta(minutes=30)


@dataclass(slots=True)
class Invoice:
    id: InvoiceId = field(repr=False)  # the credential: see InvoiceId
    created_at: Timestamp
    expires_at: Timestamp
    request: QuoteRequest
    quote: QuoteResponse
    operator: Operator
    accounts: Accounts
    funding: Funding

    status: InvoiceStatus = InvoiceStatus.QUOTED
    reason: str | None = None
    # the owner asked Mai to stop: the planner puts the tools down once nothing is in flight
    tools_down_requested: bool = False
    # how the facilitators this job used did for it: the planner tries the best first
    facilitator_reliability: dict[str, Reliability] = field(default_factory=dict)

    holdings: Holdings = field(default_factory=Holdings)
    worklog: WorkLog = field(default_factory=WorkLog)
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    # held while an x402 payment settles, so one quote is never paid for twice
    payment_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def require_funding(self, *allowed: Funding) -> None:
        if self.funding not in allowed:
            raise InvoiceFundingError(f"the invoice is funded by {self.funding}, not {allowed}")

    def check_acceptable(self) -> None:
        if self.status == InvoiceStatus.EXPIRED:
            raise InvoiceExpiredError("the quote has expired")
        if self.status != InvoiceStatus.QUOTED:
            raise InvoiceStateError(f"the quote was already accepted: it's {self.status}")
        if Timestamp.now() > self.expires_at:
            self.status = InvoiceStatus.EXPIRED
            raise InvoiceExpiredError("the quote has expired")

    def accept(self) -> None:
        self.check_acceptable()
        self.begin()

    def begin(self) -> None:
        """Start awaiting the inputs. After an x402 payment settles this can't be refused."""
        self.status = InvoiceStatus.AWAITING_DEPOSIT
        self.expires_at = Timestamp.now() + DEPOSIT_TTL

    def finish(self, status: InvoiceStatus, reason: str) -> None:
        self.status = status
        self.reason = reason

    @property
    def finished(self) -> bool:
        """Delivered, or the tools are down: nothing more will be done."""
        return self.status in (InvoiceStatus.DELIVERED, InvoiceStatus.TOOLS_DOWN)

    def observed(self, balance: Balance) -> None:
        """The chain shows `balance` at one of the job's accounts: anything beyond what the
        record accounts for there (held, or already released) arrived from outside."""
        token = balance.amount.token
        recorded = sum(
            self.holdings.amount_at(Place(address=balance.address, custody=c), token).amount.amount
            for c in (Custody.HELD, Custody.RELEASED)
        )
        if balance.amount.amount > recorded:
            arrived = TokenAmount(token=token, amount=Amount(balance.amount.amount - recorded))
            self.holdings.apply(
                Movement(
                    kind=MovementKind.INPUT,
                    amount=arrived,
                    source=None,
                    destination=Place(address=balance.address, custody=Custody.HELD),
                    timestamp=Timestamp.now(),
                )
            )

    def release(self) -> None:
        """Record that the owner now holds the keys: whatever is held is released."""
        for position in self.holdings.positions:
            if position.place.custody != Custody.HELD:
                continue
            released = Place(address=position.place.address, custody=Custody.RELEASED)
            self.holdings.apply(
                Movement(
                    kind=MovementKind.RELEASE,
                    amount=position.amount,
                    source=position.place,
                    destination=released,
                    timestamp=Timestamp.now(),
                )
            )

    def view(self) -> InvoiceView:
        return InvoiceView(
            quote_id=str(self.id),
            operator=self.operator,
            funding=self.funding,
            status=self.status,
            reason=self.reason,
            created_at=self.created_at,
            expires_at=self.expires_at,
            inputs=self.input_states(),
            outputs=self.output_states(),
            gas=self.quote.gas,
            accounts=self.accounts.all,
            holdings=self.holdings.positions,
            movements=self.holdings.movements,
            actions=tuple(action.view() for action in self.worklog.actions),
        )

    # --- Goal and progress: the goal is the quote; progress is read from the holdings ---

    @property
    def deposits(self) -> tuple[Balance, ...]:
        """Each input, at the account it's deposited to."""
        return tuple(
            Balance(amount=inp, address=self.accounts.input(i, inp.token.chain).address)
            for i, inp in enumerate(self.quote.inputs)
        )

    def input_states(self) -> tuple[InvoiceInput, ...]:
        states: list[InvoiceInput] = []
        for deposit in self.deposits:
            token = deposit.amount.token
            received = self.holdings.received(token, deposit.address)
            if received.amount.amount >= deposit.amount.amount:
                status = InputStatus.RECEIVED
            elif self.status == InvoiceStatus.EXPIRED:
                status = InputStatus.EXPIRED
            elif self.status == InvoiceStatus.TOOLS_DOWN:
                status = InputStatus.NOT_RECEIVED
            else:
                status = InputStatus.AWAITING
            states.append(
                InvoiceInput(
                    deposit=deposit,
                    received=received.amount,
                    status=status,
                )
            )
        return tuple(states)

    def output_states(self) -> tuple[InvoiceOutput, ...]:
        """Match each output to the transfers that carried exactly it (recipient and amount).

        Identical outputs are interchangeable, so any matching order is correct. An output
        claims its returned attempts and at most one live attempt (in flight or delivered).
        """
        attempts = _transfer_attempts(self.holdings.movements)
        claimed: set[int] = set()
        states: list[InvoiceOutput] = []
        for goal in self.quote.outputs:
            status = OutputStatus.PENDING
            transactions: list[Transaction] = []
            tries = 0
            for i, attempt in enumerate(attempts):
                if i in claimed or attempt.balance != goal:
                    continue
                claimed.add(i)
                tries += 1
                transactions.extend(attempt.transactions)
                if attempt.outcome == Custody.DELIVERED:
                    status = OutputStatus.DELIVERED
                    break
                if attempt.outcome == Custody.IN_FLIGHT:
                    status = OutputStatus.SUBMITTED
                    break
            states.append(
                InvoiceOutput(
                    balance=goal,
                    status=status,
                    transactions=tuple(transactions),
                    attempts=tries,
                )
            )
        return tuple(states)

    @property
    def is_expired(self) -> bool:
        return Timestamp.now() > self.expires_at and self.status in (
            InvoiceStatus.QUOTED,
            InvoiceStatus.AWAITING_DEPOSIT,
        )

    @property
    def is_delivered(self) -> bool:
        return all(out.status == OutputStatus.DELIVERED for out in self.output_states())

    @property
    def current_action(self) -> Action | None:
        for action in reversed(self.worklog.actions):
            if not action.is_complete:
                return action
        return None

    @property
    def is_active(self) -> bool:
        return self.task is not None and not self.task.done()


@dataclass(frozen=True, slots=True)
class _TransferAttempt:
    balance: Balance
    transactions: tuple[Transaction, ...]
    outcome: Custody  # IN_FLIGHT, DELIVERED, or HELD when it came back


def _transfer_attempts(movements: tuple[Movement, ...]) -> list[_TransferAttempt]:
    """Every transfer sent (held -> in flight, by a work-log step), with where its funds
    ended up and the on-chain transactions involved."""
    outcomes: dict[StepId, Custody] = {}
    transactions: dict[StepId, dict[TxHash, Transaction]] = {}
    for m in movements:
        for place in (m.source, m.destination):
            if place is None or place.custody != Custody.IN_FLIGHT or place.step is None:
                continue
            if m.transaction is not None:
                tx = Transaction(
                    chain=place.address.chain, hash=m.transaction, timestamp=m.timestamp
                )
                transactions.setdefault(place.step, {}).setdefault(m.transaction, tx)
        if m.source is not None and m.source.custody == Custody.IN_FLIGHT and m.source.step:
            outcomes[m.source.step] = m.destination.custody
    attempts: list[_TransferAttempt] = []
    for m in movements:
        ref = m.destination.step
        if m.kind != MovementKind.TRANSIT or m.destination.custody != Custody.IN_FLIGHT or not ref:
            continue
        attempts.append(
            _TransferAttempt(
                balance=Balance(amount=m.amount, address=m.destination.address),
                transactions=tuple(transactions.get(ref, {}).values()),
                outcome=outcomes.get(ref, Custody.IN_FLIGHT),
            )
        )
    return attempts


# --- Registry ---


class InvoiceRegistry:
    def __init__(self, operator: Operator) -> None:
        self._operator = operator
        self._invoices: dict[InvoiceId, Invoice] = {}

    def create_quote(
        self, request: QuoteRequest, quote: QuoteResponse, accounts: Accounts, funding: Funding
    ) -> Invoice:
        inv_id = InvoiceId(quote.quote_id)
        now = Timestamp.now()
        invoice = Invoice(
            id=inv_id,
            created_at=now,
            expires_at=now + QUOTE_TTL,
            request=request,
            quote=quote,
            operator=self._operator,
            accounts=accounts,
            funding=funding,
        )
        self._invoices[inv_id] = invoice
        return invoice

    def get(self, invoice_id: InvoiceId) -> Invoice:
        invoice = self._invoices.get(invoice_id)
        if invoice is None:
            raise InvoiceNotFoundError("no such invoice")
        return invoice

    def all(self) -> list[Invoice]:
        return list(self._invoices.values())

    def active(self) -> list[Invoice]:
        return [inv for inv in self._invoices.values() if inv.is_active]

    def reap_expired(self) -> int:
        expired = [
            inv_id
            for inv_id, inv in self._invoices.items()
            if inv.is_expired and inv.status == InvoiceStatus.QUOTED
        ]
        for inv_id in expired:
            self._invoices[inv_id].status = InvoiceStatus.EXPIRED
        return len(expired)
