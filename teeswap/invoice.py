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

from .blockchain.chains import Chain
from .blockchain.evm import EthSigner
from .common import TeeSwapError
from .config import Operator
from .protocol import Protocol
from .response import DataclassResponse
from .types import (
    Address,
    Amount,
    Balance,
    HttpExchange,
    QuoteRequest,
    QuoteResponse,
    Timestamp,
    TokenAmount,
    Transaction,
)
from .wire import WireStruct


class InvoiceError(TeeSwapError):
    pass


class InvoiceNotFoundError(InvoiceError):
    pass


class InvoiceExpiredError(InvoiceError):
    pass


class InvoiceStateError(InvoiceError):
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
    protocol: str | None
    source_chain: Chain
    destination_chain: Chain | None
    steps: tuple[StepView, ...]


@dataclass(slots=True)
class Action:
    protocol: Protocol | None
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
            protocol=self.protocol.meta.name if self.protocol else None,
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


class InputStatus(enum.StrEnum):
    AWAITING = "awaiting"
    RECEIVED = "received"
    EXPIRED = "expired"


class OutputStatus(enum.StrEnum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class InvoiceInput(WireStruct):
    deposit: Balance
    received: TokenAmount
    status: InputStatus = InputStatus.AWAITING


@dataclass(frozen=True, slots=True)
class InvoiceOutput(WireStruct):
    balance: Balance
    status: OutputStatus = OutputStatus.PENDING
    transactions: tuple[Transaction, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class InvoiceView(DataclassResponse):
    """The public invoice: what the invoice page shows, as data."""

    quote_id: str
    operator: Operator
    status: InvoiceStatus
    created_at: Timestamp
    expires_at: Timestamp
    inputs: tuple[InvoiceInput, ...]
    outputs: tuple[InvoiceOutput, ...]
    gas: TokenAmount
    fee: TokenAmount
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
    inputs: list[InvoiceInput]
    outputs: list[InvoiceOutput]

    status: InvoiceStatus = InvoiceStatus.QUOTED

    actions: list[Action] = field(default_factory=list)
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    def accept(self) -> None:
        if self.status != InvoiceStatus.QUOTED:
            raise InvoiceStateError(f"cannot accept invoice in state {self.status}")
        if Timestamp.now() > self.expires_at:
            self.status = InvoiceStatus.EXPIRED
            raise InvoiceExpiredError(self.id)
        self.status = InvoiceStatus.AWAITING_DEPOSIT
        self.expires_at = Timestamp.now() + DEPOSIT_TTL

    def view(self) -> InvoiceView:
        return InvoiceView(
            quote_id=str(self.id),
            operator=self.operator,
            status=self.status,
            created_at=self.created_at,
            expires_at=self.expires_at,
            inputs=tuple(self.inputs),
            outputs=tuple(self.outputs),
            gas=self.quote.gas,
            fee=self.quote.fee,
            actions=tuple(action.view() for action in self.actions),
        )

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


# --- Registry ---


class InvoiceRegistry:
    def __init__(self, operator: Operator) -> None:
        self._operator = operator
        self._invoices: dict[InvoiceId, Invoice] = {}

    def create_quote(
        self, request: QuoteRequest, quote: QuoteResponse, signer: EthSigner
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
            inputs=[
                InvoiceInput(
                    deposit=Balance(amount=inp, address=Address(inp.token.chain, signer.address)),
                    received=TokenAmount(token=inp.token, amount=Amount(0)),
                )
                for inp in quote.inputs
            ],
            outputs=[InvoiceOutput(balance=out) for out in quote.outputs],
        )
        self._invoices[inv_id] = invoice
        return invoice

    def get(self, invoice_id: InvoiceId) -> Invoice:
        invoice = self._invoices.get(invoice_id)
        if invoice is None:
            raise InvoiceNotFoundError(invoice_id)
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
