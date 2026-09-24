"""End-to-end flows: what a user does, through each interface, against a real chain.

Each test runs once per interface (see conftest.api). Deposits are real transfers
from a funded account; x402 payments are signed by the user's wallet and settled by
the local facilitator.
"""

import asyncio

import pytest

from teeswap.api import Api
from teeswap.execution.invoice import InputStatus, InvoiceNotFoundError, InvoiceView, OutputStatus
from teeswap.execution.ledger import Custody
from teeswap.execution.planner import NoRouteError
from teeswap.tests.conftest import (
    ETH,
    ONE_ETH,
    ONE_USDC,
    OPERATOR_NAME,
    USDC_TOKEN,
    SaveInvoice,
    Served,
    new_address,
)
from teeswap.tests.localchain import LocalChain
from teeswap.tools import StatusResponse
from teeswap.types import (
    Address,
    Amount,
    Balance,
    InvoiceRequest,
    QuoteRequest,
    QuoteResponse,
    TokenAmount,
)
from teeswap.wire import decode_object
from teeswap.x402 import EvmPayer

SURPLUS = 10**15  # 0.001 ETH
FINISHED = ("delivered", "failed", "expired", "halted")


def _to(address: str, amount: TokenAmount) -> Balance:
    return Balance(amount=amount, address=Address(amount.token.chain, address))


async def _until_finished(api: Api, quote_id: str) -> StatusResponse:
    request = InvoiceRequest(quote_id=quote_id)
    status = await api.status(request)
    for _ in range(120):
        if status.status in FINISHED:
            break
        await asyncio.sleep(0.5)
        status = await api.status(request)
    return status


async def _quote_x402_when_routable(api: Api, request: QuoteRequest) -> QuoteResponse:
    """A client's view of a TeeSwap that has just started: no route until its
    facilitators have been seen healthy."""
    for _ in range(50):
        try:
            return await api.quote_x402(request)
        except NoRouteError:
            await asyncio.sleep(0.1)
    return await api.quote_x402(request)


def _by_custody(view: InvoiceView) -> dict[Custody, int]:
    totals: dict[Custody, int] = {}
    for position in view.holdings:
        custody = position.place.custody
        totals[custody] = totals.get(custody, 0) + position.amount.amount
    return totals


@pytest.mark.asyncio
async def test_eth_split(
    api: Api, served: Served, chain: LocalChain, invoice_page: SaveInvoice
) -> None:
    recipient_a, recipient_b = new_address(), new_address()
    request = QuoteRequest(
        inputs=(
            TokenAmount(token=ETH, amount=Amount(ONE_ETH * 40 // 100)),
            TokenAmount(token=ETH, amount=Amount(ONE_ETH * 60 // 100)),
        ),
        outputs=(
            _to(recipient_a, TokenAmount(token=ETH, amount=Amount(ONE_ETH * 60 // 100))),
            _to(recipient_b, TokenAmount(token=ETH, amount=Amount(ONE_ETH * 40 // 100))),
        ),
    )
    quote = await api.quote(request)
    assert quote.inputs == (TokenAmount(token=ETH, amount=Amount(ONE_ETH)),)

    accepted = await api.accept(InvoiceRequest(quote_id=quote.quote_id))
    (deposit_to,) = accepted.deposits
    # a little over the quote: Mai carries on and the surplus stays held
    paid = ONE_ETH + SURPLUS
    chain.transfer_eth(deposit_to.address.value, paid)

    status = await _until_finished(api, quote.quote_id)
    assert status.status == "delivered", f"invoice ended {status.status}"
    (deposit,) = status.inputs
    assert deposit.status == InputStatus.RECEIVED
    assert deposit.deposit.address == deposit_to.address
    assert deposit.received.amount == paid
    assert [o.status for o in status.outputs] == [OutputStatus.DELIVERED] * 2
    assert all(len(o.transactions) == 1 for o in status.outputs)
    assert chain.eth_balance(recipient_a) > chain.eth_balance(recipient_b) > 0

    view = await api.invoice(InvoiceRequest(quote_id=quote.quote_id))
    # nothing went wrong, so Mai did exactly what her provisional plan said
    assert tuple(a.description for a in view.actions) == quote.plan
    assert view.outputs == status.outputs

    # custody: every wei that arrived is held, delivered or consumed, and the chain agrees
    by_custody = _by_custody(view)
    assert sum(by_custody.values()) == paid
    assert by_custody.get(Custody.IN_FLIGHT, 0) == 0
    delivered = {
        p.place.address.value: p.amount.amount
        for p in view.holdings
        if p.place.custody == Custody.DELIVERED
    }
    assert delivered == {o.balance.address.value: o.balance.amount.amount for o in view.outputs}
    assert by_custody[Custody.HELD] == chain.eth_balance(deposit_to.address.value)
    assert by_custody[Custody.HELD] >= SURPLUS

    # the invoice pages show the same invoice
    html = await invoice_page(quote.quote_id)
    assert "DELIVERED" in html
    assert OPERATOR_NAME in html
    assert "MST: 0123456789" in html
    assert deposit_to.address.value in html
    json = await served.client.get(f"/invoice/{quote.quote_id}.json")
    assert json.status_code == 200
    assert InvoiceView.from_dict(decode_object(json.content)) == view


@pytest.mark.asyncio
async def test_usdc_split_paid_by_x402(
    api: Api, payer: EvmPayer, chain: LocalChain, invoice_page: SaveInvoice
) -> None:
    recipient_a, recipient_b = new_address(), new_address()
    request = QuoteRequest(
        inputs=(TokenAmount(token=USDC_TOKEN, amount=Amount(5 * ONE_USDC)),),
        outputs=(
            _to(recipient_a, TokenAmount(token=USDC_TOKEN, amount=Amount(3 * ONE_USDC))),
            _to(recipient_b, TokenAmount(token=USDC_TOKEN, amount=Amount(2 * ONE_USDC))),
        ),
    )
    quote = await _quote_x402_when_routable(api, request)
    # the facilitator pays the gas: the outputs are exactly what was asked for
    assert quote.outputs == request.outputs

    paid = await api.accept_paid(InvoiceRequest(quote_id=quote.quote_id), payer)
    assert paid.settlement.success
    assert paid.settlement.transaction in paid.accepted.instructions

    status = await _until_finished(api, quote.quote_id)
    assert status.status == "delivered", f"invoice ended {status.status}"
    (deposit,) = status.inputs
    assert deposit.status == InputStatus.RECEIVED
    assert deposit.received.amount == 5 * ONE_USDC
    assert [o.status for o in status.outputs] == [OutputStatus.DELIVERED] * 2
    assert chain.usdc_balance(payer.address) == 5 * ONE_USDC
    assert chain.usdc_balance(recipient_a) == 3 * ONE_USDC
    assert chain.usdc_balance(recipient_b) == 2 * ONE_USDC

    # custody: everything that came in was delivered, nothing is left held or in flight
    by_custody = _by_custody(await api.invoice(InvoiceRequest(quote_id=quote.quote_id)))
    assert by_custody.get(Custody.DELIVERED) == 5 * ONE_USDC
    assert not by_custody.get(Custody.HELD) and not by_custody.get(Custody.IN_FLIGHT)
    assert chain.usdc_balance(deposit.deposit.address.value) == 0
    await invoice_page(quote.quote_id)


@pytest.mark.asyncio
async def test_unknown_invoice(api: Api) -> None:
    """A mistyped quote id is the same error whichever way it's asked."""
    with pytest.raises(InvoiceNotFoundError):
        await api.status(InvoiceRequest(quote_id="inv_bogus"))
