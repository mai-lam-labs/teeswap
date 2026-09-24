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
import secrets
from dataclasses import dataclass, field
from datetime import timedelta

from ..common import TeeSwapError
from ..config import Operator
from ..response import DataclassResponse
from ..types import (
    Balance,
    QuoteRequest,
    QuoteResponse,
    Timestamp,
    TokenAmount,
    Transaction,
    TxHash,
)
from ..wire import WireStruct
from .accounts import Accounts
from .ledger import Custody, Holdings, Movement, MovementKind, Position
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
    @classmethod
    def generate(cls) -> InvoiceId:
        return cls("inv_" + secrets.token_hex(12))


# --- Invoice ---


class InvoiceStatus(enum.StrEnum):
    QUOTED = "quoted"
    AWAITING_DEPOSIT = "awaiting_deposit"
    EXECUTING = "executing"
    DELIVERED = "delivered"
    FAILED = "failed"  # the planner found no way to finish the job
    EXPIRED = "expired"
    HALTED = "halted"  # stopped by the engine's guardrails: needs a human


class Funding(enum.StrEnum):
    """How the job's inputs reach its address; chosen at quote time."""

    DEPOSIT = "deposit"  # the client transfers them
    X402 = "x402"  # an x402 payment, settled by a facilitator, delivers them


class InputStatus(enum.StrEnum):
    AWAITING = "awaiting"
    RECEIVED = "received"
    EXPIRED = "expired"


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
    holdings: tuple[Position, ...]
    movements: tuple[Movement, ...]
    actions: tuple[ActionView, ...]


QUOTE_TTL = timedelta(minutes=5)
DEPOSIT_TTL = timedelta(minutes=30)


@dataclass(slots=True)
class Invoice:
    id: InvoiceId
    created_at: Timestamp
    expires_at: Timestamp
    request: QuoteRequest
    quote: QuoteResponse
    operator: Operator
    accounts: Accounts
    funding: Funding

    status: InvoiceStatus = InvoiceStatus.QUOTED
    reason: str | None = None
    # facilitators that failed an operation for this job; the planner won't pick them again
    excluded_facilitators: set[str] = field(default_factory=set)

    holdings: Holdings = field(default_factory=Holdings)
    worklog: WorkLog = field(default_factory=WorkLog)
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    # held while an x402 payment settles, so one quote is never paid for twice
    payment_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def require_funding(self, funding: Funding) -> None:
        if self.funding != funding:
            raise InvoiceFundingError(
                f"invoice {self.id} is funded by {self.funding}, not {funding}"
            )

    def check_acceptable(self) -> None:
        if self.status != InvoiceStatus.QUOTED:
            raise InvoiceStateError(f"cannot accept invoice in state {self.status}")
        if Timestamp.now() > self.expires_at:
            self.status = InvoiceStatus.EXPIRED
            raise InvoiceExpiredError(f"quote {self.id} has expired")

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
            raise InvoiceNotFoundError(f"no invoice {invoice_id}")
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
