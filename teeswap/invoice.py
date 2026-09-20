import enum
import time
from dataclasses import dataclass, field

from .common import TeeSwapError


class InvoiceError(TeeSwapError):
    pass


class InvoiceNotFoundError(InvoiceError):
    pass


class InvoiceStateError(InvoiceError):
    pass


class InvoiceStatus(enum.StrEnum):
    DRAFT = "draft"
    SETTLING = "settling"
    PAID = "paid"
    FULFILLED = "fulfilled"
    FAILED = "failed"
    REFUNDED = "refunded"


INVOICE_TRANSITIONS: dict[InvoiceStatus, frozenset[InvoiceStatus]] = {
    InvoiceStatus.DRAFT: frozenset({InvoiceStatus.SETTLING}),
    InvoiceStatus.SETTLING: frozenset({InvoiceStatus.PAID, InvoiceStatus.FAILED}),
    InvoiceStatus.PAID: frozenset({InvoiceStatus.FULFILLED}),
    InvoiceStatus.FULFILLED: frozenset({InvoiceStatus.REFUNDED}),
    InvoiceStatus.FAILED: frozenset({InvoiceStatus.SETTLING}),
    InvoiceStatus.REFUNDED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class PaymentRequirement:
    scheme: str
    network: str
    amount: str
    currency: str
    pay_to: str
    description: str


@dataclass(frozen=True, slots=True)
class PaymentRecord:
    chain_id: int
    from_address: str
    to_address: str
    tx_hash: str
    amount: str
    currency: str
    settled_at: float


@dataclass(slots=True)
class Invoice:
    invoice_id: str
    tool_name: str
    requirement: PaymentRequirement
    status: InvoiceStatus = InvoiceStatus.DRAFT
    created_at: float = field(default_factory=time.time)
    payment: PaymentRecord | None = None
    fulfilled_at: float | None = None
    refunded_at: float | None = None

    def transition(self, to: InvoiceStatus) -> None:
        allowed = INVOICE_TRANSITIONS.get(self.status, frozenset())
        if to not in allowed:
            raise InvoiceStateError(f"{self.status.value} -> {to.value} not allowed")
        self.status = to

    @property
    def is_terminal(self) -> bool:
        return self.status in (InvoiceStatus.FULFILLED, InvoiceStatus.REFUNDED)


@dataclass(frozen=True, slots=True)
class InvoiceEvent:
    invoice_id: str
    tool_name: str
    status: InvoiceStatus
    created_at: float
    requirement: PaymentRequirement
    payment: PaymentRecord | None
    fulfilled_at: float | None
    refunded_at: float | None


def terminal_event(invoice: Invoice) -> InvoiceEvent:
    return InvoiceEvent(
        invoice_id=invoice.invoice_id,
        tool_name=invoice.tool_name,
        status=invoice.status,
        created_at=invoice.created_at,
        requirement=invoice.requirement,
        payment=invoice.payment,
        fulfilled_at=invoice.fulfilled_at,
        refunded_at=invoice.refunded_at,
    )


class InvoiceStore:
    def __init__(self) -> None:
        self._invoices: dict[str, Invoice] = {}

    def create(
        self,
        invoice_id: str,
        tool_name: str,
        requirement: PaymentRequirement,
    ) -> Invoice:
        invoice = Invoice(
            invoice_id=invoice_id,
            tool_name=tool_name,
            requirement=requirement,
        )
        self._invoices[invoice_id] = invoice
        return invoice

    def get(self, invoice_id: str) -> Invoice:
        invoice = self._invoices.get(invoice_id)
        if invoice is None:
            raise InvoiceNotFoundError(invoice_id)
        return invoice

    def list_for_session(self) -> list[Invoice]:
        return list(self._invoices.values())
