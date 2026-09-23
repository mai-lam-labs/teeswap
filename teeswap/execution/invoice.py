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

from ..blockchain.chains import Chain
from ..blockchain.evm import EthSigner
from ..common import TeeSwapError
from ..config import Operator
from ..response import DataclassResponse
from ..types import (
    Address,
    Amount,
    Balance,
    Hex32,
    HttpExchange,
    QuoteRequest,
    QuoteResponse,
    Timestamp,
    TokenAmount,
    Transaction,
    TxHash,
)
from ..wire import WireStruct
from .ledger import Custody, Holdings, Movement, MovementKind, Place, Position


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


# --- Work log ---


class StepStatus(enum.StrEnum):
    PENDING = "pending"
    EXECUTING = "executing"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class StepView(WireStruct):
    operation: str
    status: StepStatus
    chain: Chain | None
    started_at: Timestamp | None
    completed_at: Timestamp | None
    error: str | None
    transactions: tuple[Transaction, ...]


@dataclass(slots=True)
class Step:
    operation: str
    chain: Chain | None
    status: StepStatus = StepStatus.PENDING
    started_at: Timestamp | None = None
    completed_at: Timestamp | None = None
    transactions: list[Transaction] = field(default_factory=list)
    http_exchanges: list[HttpExchange] = field(default_factory=list)
    error: str | None = None

    def start(self) -> None:
        self.status = StepStatus.EXECUTING
        self.started_at = Timestamp.now()

    def wait(self) -> None:
        self.status = StepStatus.WAITING

    def complete(self) -> None:
        self.status = StepStatus.COMPLETED
        self.completed_at = Timestamp.now()

    def fail(self, error: str) -> None:
        self.status = StepStatus.FAILED
        self.completed_at = Timestamp.now()
        self.error = error

    def view(self) -> StepView:
        return StepView(
            operation=self.operation,
            status=self.status,
            chain=self.chain,
            started_at=self.started_at,
            completed_at=self.completed_at,
            error=self.error,
            transactions=tuple(self.transactions),
        )


@dataclass(frozen=True, slots=True)
class ActionView(WireStruct):
    description: str
    source_chain: Chain
    destination_chain: Chain | None
    steps: tuple[StepView, ...]


@dataclass(slots=True)
class Action:
    description: str
    source_chain: Chain
    destination_chain: Chain | None
    started_at: Timestamp = field(default_factory=Timestamp.now)
    steps: list[Step] = field(default_factory=list)

    def add_step(self, operation: str, chain: Chain | None = None) -> Step:
        step = Step(operation=operation, chain=chain or self.source_chain)
        self.steps.append(step)
        return step

    @property
    def current_step(self) -> Step | None:
        for step in reversed(self.steps):
            if step.status in (StepStatus.EXECUTING, StepStatus.WAITING):
                return step
        return None

    @property
    def is_complete(self) -> bool:
        return bool(self.steps) and all(
            s.status in (StepStatus.COMPLETED, StepStatus.FAILED) for s in self.steps
        )

    def view(self) -> ActionView:
        return ActionView(
            description=self.description,
            source_chain=self.source_chain,
            destination_chain=self.destination_chain,
            steps=tuple(step.view() for step in self.steps),
        )


# --- Invoice ---


class InvoiceStatus(enum.StrEnum):
    QUOTED = "quoted"
    AWAITING_DEPOSIT = "awaiting_deposit"
    EXECUTING = "executing"
    DELIVERED = "delivered"
    FAILED = "failed"
    EXPIRED = "expired"


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


@dataclass(frozen=True)
class InvoiceView(DataclassResponse):
    """The public invoice: what the invoice page shows, as data."""

    quote_id: str
    operator: Operator
    funding: Funding
    status: InvoiceStatus
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
    signer: EthSigner
    funding: Funding

    status: InvoiceStatus = InvoiceStatus.QUOTED
    # facilitators that failed an operation for this job; the planner won't pick them again
    excluded_facilitators: set[str] = field(default_factory=set)

    holdings: Holdings = field(default_factory=Holdings)
    actions: list[Action] = field(default_factory=list)
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

    def view(self) -> InvoiceView:
        return InvoiceView(
            quote_id=str(self.id),
            operator=self.operator,
            funding=self.funding,
            status=self.status,
            created_at=self.created_at,
            expires_at=self.expires_at,
            inputs=self.input_states(),
            outputs=self.output_states(),
            gas=self.quote.gas,
            holdings=self.holdings.positions,
            movements=self.holdings.movements,
            actions=tuple(action.view() for action in self.actions),
        )

    # --- Goal and progress: the goal is the quote; progress is read from the holdings ---

    def job_address(self, chain: Chain) -> Address:
        """The address the job's key controls on `chain`."""
        return Address(chain, self.signer.address)

    def held(self, chain: Chain) -> Place:
        return Place(address=self.job_address(chain), custody=Custody.HELD)

    @property
    def deposits(self) -> tuple[Balance, ...]:
        return tuple(
            Balance(amount=inp, address=self.job_address(inp.token.chain))
            for inp in self.quote.inputs
        )

    def input_states(self) -> tuple[InvoiceInput, ...]:
        states: list[InvoiceInput] = []
        for deposit in self.deposits:
            token = deposit.amount.token
            received = self.holdings.received(token, deposit.address)
            if received >= deposit.amount.amount:
                status = InputStatus.RECEIVED
            elif self.status == InvoiceStatus.EXPIRED:
                status = InputStatus.EXPIRED
            else:
                status = InputStatus.AWAITING
            states.append(
                InvoiceInput(
                    deposit=deposit,
                    received=TokenAmount(token=token, amount=Amount(received)),
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
            for i, attempt in enumerate(attempts):
                if i in claimed or attempt.balance != goal:
                    continue
                claimed.add(i)
                transactions.extend(attempt.transactions)
                if attempt.outcome == Custody.DELIVERED:
                    status = OutputStatus.DELIVERED
                    break
                if attempt.outcome == Custody.IN_FLIGHT:
                    status = OutputStatus.SUBMITTED
                    break
            states.append(
                InvoiceOutput(balance=goal, status=status, transactions=tuple(transactions))
            )
        return tuple(states)

    @property
    def is_expired(self) -> bool:
        return Timestamp.now() > self.expires_at and self.status in (
            InvoiceStatus.QUOTED,
            InvoiceStatus.AWAITING_DEPOSIT,
        )

    @property
    def current_action(self) -> Action | None:
        for action in reversed(self.actions):
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
    """Every transfer sent (held -> in flight), with where its funds ended up and the
    on-chain transactions involved."""
    outcomes: dict[Hex32, Custody] = {}
    transactions: dict[Hex32, dict[TxHash, Transaction]] = {}
    for m in movements:
        for place in (m.source, m.destination):
            if place is None or place.custody != Custody.IN_FLIGHT or place.reference is None:
                continue
            if m.transaction is not None:
                tx = Transaction(
                    chain=place.address.chain, hash=m.transaction, timestamp=m.timestamp
                )
                transactions.setdefault(place.reference, {}).setdefault(m.transaction, tx)
        if m.source is not None and m.source.custody == Custody.IN_FLIGHT and m.source.reference:
            outcomes[m.source.reference] = m.destination.custody
    attempts: list[_TransferAttempt] = []
    for m in movements:
        ref = m.destination.reference
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
        self, request: QuoteRequest, quote: QuoteResponse, signer: EthSigner, funding: Funding
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
            signer=signer,
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
