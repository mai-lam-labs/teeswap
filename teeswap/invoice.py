"""Invoice — the append-only record of work performed for a user.

Lifecycle: quote (stateless estimate with TTL) → accept (signer derived,
deposit address assigned, coroutine started) → funded → executing → delivered.

One ID throughout. The quote IS the invoice before it's accepted.

See docs/INVOICING.md for the design principles.
"""

import asyncio
import enum
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .blockchain.chains import Chain
from .blockchain.evm import EthSigner
from .common import TeeSwapError
from .protocol import Protocol
from .types import HttpExchange, QuoteRequest, QuoteResponse, Transaction


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


@dataclass(slots=True)
class Step:
    operation: str
    chain: Chain | None
    status: StepStatus = StepStatus.PENDING
    started_at: datetime | None = None
    completed_at: datetime | None = None
    transactions: list[Transaction] = field(default_factory=list)
    http_exchanges: list[HttpExchange] = field(default_factory=list)
    error: str | None = None

    def start(self) -> None:
        self.status = StepStatus.EXECUTING
        self.started_at = datetime.now(UTC)

    def wait(self) -> None:
        self.status = StepStatus.WAITING

    def complete(self) -> None:
        self.status = StepStatus.COMPLETED
        self.completed_at = datetime.now(UTC)

    def fail(self, error: str) -> None:
        self.status = StepStatus.FAILED
        self.completed_at = datetime.now(UTC)
        self.error = error


@dataclass(slots=True)
class Action:
    protocol: Protocol | None
    description: str
    source_chain: Chain
    destination_chain: Chain | None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
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


# --- Invoice ---


class InvoiceStatus(enum.StrEnum):
    QUOTED = "quoted"
    AWAITING_DEPOSIT = "awaiting_deposit"
    EXECUTING = "executing"
    DELIVERED = "delivered"
    FAILED = "failed"
    EXPIRED = "expired"


QUOTE_TTL = timedelta(minutes=5)
DEPOSIT_TTL = timedelta(minutes=30)


@dataclass(slots=True)
class Invoice:
    id: InvoiceId
    created_at: datetime
    expires_at: datetime
    request: QuoteRequest
    quote: QuoteResponse
    source_chain: Chain

    status: InvoiceStatus = InvoiceStatus.QUOTED

    signer: EthSigner | None = None
    deposit_address: str | None = None
    actions: list[Action] = field(default_factory=list)
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    def accept(self, signer: EthSigner) -> None:
        if self.status != InvoiceStatus.QUOTED:
            raise InvoiceStateError(f"cannot accept invoice in state {self.status}")
        if datetime.now(UTC) > self.expires_at:
            self.status = InvoiceStatus.EXPIRED
            raise InvoiceExpiredError(self.id)
        self.signer = signer
        self.deposit_address = signer.address
        self.status = InvoiceStatus.AWAITING_DEPOSIT
        self.expires_at = datetime.now(UTC) + DEPOSIT_TTL

    @property
    def is_expired(self) -> bool:
        return datetime.now(UTC) > self.expires_at and self.status in (
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
    def __init__(self) -> None:
        self._invoices: dict[InvoiceId, Invoice] = {}

    def create_quote(self, request: QuoteRequest, quote: QuoteResponse, chain: Chain) -> Invoice:
        inv_id = InvoiceId(quote.quote_id)
        now = datetime.now(UTC)
        invoice = Invoice(
            id=inv_id,
            created_at=now,
            expires_at=now + QUOTE_TTL,
            request=request,
            quote=quote,
            source_chain=chain,
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
