"""The Api interface (see __init__.py)."""

import abc

from ..execution.invoice import Handover, InvoiceView
from ..tools import StatusResponse
from ..types import (
    AcceptResponse,
    InvoiceRequest,
    KeysQuoteRequest,
    QuoteRequest,
    QuoteResponse,
)
from ..x402 import PaidResponse, Payer, PaymentPayload, PaymentRequired


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
    async def payment_required(self, request: InvoiceRequest) -> PaymentRequired:
        """What paying for an x402-funded quote takes: teeswap_accept_x402, unpaid."""

    @abc.abstractmethod
    async def accept_x402(
        self, request: InvoiceRequest, payment: PaymentPayload
    ) -> PaidResponse[AcceptResponse]:
        """Pay for an x402-funded quote and start it: the accepted invoice, and how the
        payment settled. PaymentNotSettledError says why, if it wasn't taken."""

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

    async def accept_paid(
        self, request: InvoiceRequest, payer: Payer
    ) -> PaidResponse[AcceptResponse]:
        """Accept an x402-funded quote, paying for it: ask what to pay, pay, call again."""
        payment = await payer.pay(await self.payment_required(request))
        return await self.accept_x402(request, payment)
