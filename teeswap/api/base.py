"""The Api interface (see __init__.py)."""

import abc
from dataclasses import dataclass

from ..execution.invoice import Handover, InvoiceView
from ..tools import StatusResponse
from ..types import (
    AcceptResponse,
    InvoiceRequest,
    KeysQuoteRequest,
    QuoteRequest,
    QuoteResponse,
)
from ..x402 import (
    Payer,
    PaymentNotSettledError,
    PaymentPayload,
    PaymentRequired,
    SettleResponse,
)


@dataclass(frozen=True, slots=True)
class PaidAccept:
    """A paid teeswap_accept_x402: the accepted invoice, and how its payment settled."""

    accepted: AcceptResponse
    settlement: SettleResponse


class Api(abc.ABC):
    @abc.abstractmethod
    async def quote(self, request: QuoteRequest) -> QuoteResponse: ...

    @abc.abstractmethod
    async def accept(self, request: InvoiceRequest) -> AcceptResponse: ...

    @abc.abstractmethod
    async def quote_x402(self, request: QuoteRequest) -> QuoteResponse: ...

    @abc.abstractmethod
    async def quote_keys(self, request: KeysQuoteRequest) -> QuoteResponse:
        """Quote with inputs in accounts whose keys you hand over. Secret, so never over REST."""

    @abc.abstractmethod
    async def accept_x402(
        self, request: InvoiceRequest, payment: PaymentPayload | None
    ) -> PaymentRequired | PaidAccept:
        """Without a payment: what to pay. With one: the settled payment and the accepted
        invoice, or what to pay again (its `error` says why it wasn't settled)."""

    @abc.abstractmethod
    async def status(self, request: InvoiceRequest) -> StatusResponse: ...

    @abc.abstractmethod
    async def invoice(self, request: InvoiceRequest) -> InvoiceView: ...

    @abc.abstractmethod
    async def tools_down(self, request: InvoiceRequest) -> StatusResponse:
        """Ask Mai to stop; the invoice reaches tools_down once nothing is in flight."""

    @abc.abstractmethod
    async def handover(self, request: InvoiceRequest) -> Handover:
        """With the tools down: the accounts and their keys. Secret, so never over REST."""

    async def accept_paid(self, request: InvoiceRequest, payer: Payer) -> PaidAccept:
        """Accept an x402-funded quote, paying for it: ask what to pay, pay, call again."""
        required = await self.accept_x402(request, None)
        if isinstance(required, PaidAccept):
            return required
        outcome = await self.accept_x402(request, await payer.pay(required))
        if isinstance(outcome, PaymentRequired):
            raise PaymentNotSettledError(outcome.error)
        return outcome
