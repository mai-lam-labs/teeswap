"""End-to-end engine tests against a local anvil node and x402 facilitator.

Requires: make anvil-start facilitator-start (make check does both)
"""

import asyncio
import base64
import os
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from eth_utils.address import to_checksum_address
from litestar import Litestar
from litestar.testing import AsyncTestClient

from teeswap.app import make_http_app
from teeswap.blockchain.chains import Chain, ChainFamily
from teeswap.blockchain.evm import EthSigner, EvmRpcClient, TransferAuthorization
from teeswap.blockchain.rpc import RpcConfig, jsonrpc
from teeswap.config import FacilitatorConfig, Operator, TeeSwapConfig
from teeswap.execution.invoice import InputStatus, InvoiceView, OutputStatus
from teeswap.execution.ledger import Custody
from teeswap.http import HttpClient
from teeswap.instance import TeeSwap
from teeswap.tests.localchain import USDC, LocalChain
from teeswap.tools import StatusResponse
from teeswap.types import (
    AcceptResponse,
    Address,
    Amount,
    Balance,
    Hex32,
    InvoiceRequest,
    QuoteRequest,
    QuoteResponse,
    Timestamp,
    Token,
    TokenAmount,
)
from teeswap.wire import decode_object, encode
from teeswap.x402 import (
    HTTP_PAYMENT_REQUIRED_HEADER,
    HTTP_PAYMENT_RESPONSE_HEADER,
    HTTP_PAYMENT_SIGNATURE_HEADER,
    X402_VERSION,
    PaymentPayload,
    PaymentRequired,
    SettleResponse,
)

ANVIL_URL = "http://127.0.0.1:8545"
ANVIL_CHAIN = Chain(
    caip2="eip155:31337",
    family=ChainFamily.EVM,
    name="Anvil",
    short_name="anvil",
    native_token="ETH",
    native_decimals=18,
    testnet=True,
)
ETH = Token(symbol="ETH", chain=ANVIL_CHAIN, contract=None, decimals=18)
USDC_TOKEN = Token(symbol=USDC.symbol, chain=ANVIL_CHAIN, contract=USDC.address, decimals=6)
FACILITATOR_URL = "http://127.0.0.1:18080"
ONE_USDC = 10**6
ROOT_KEY = b"\xbb" * 32
ONE_ETH = 10**18
SURPLUS = 10**15  # 0.001 ETH
OPERATOR_NAME = "CÔNG TY TNHH THỐNG TRỊ TOÀN CẦU MAI LÀM"


class Anvil:
    def __init__(self, url: str = ANVIL_URL) -> None:
        self.url = url
        self.client = HttpClient()
        self.rpc = EvmRpcClient(self.client, url)

    async def close(self) -> None:
        await self.client.__aexit__(None, None, None)

    async def set_balance(self, address: str, amount_wei: int) -> None:
        await jsonrpc(self.client, self.url, "anvil_setBalance", [address, hex(amount_wei)])

    async def mine(self) -> None:
        await jsonrpc(self.client, self.url, "evm_mine")


@pytest_asyncio.fixture
async def anvil():
    node = Anvil()
    try:
        await node.rpc.chain_id()
    except httpx.ConnectError as e:
        raise RuntimeError("anvil not running — run make anvil-start") from e
    yield node
    await node.close()


@pytest.mark.asyncio
async def test_eth_split_via_api(anvil: Anvil):
    instance = TeeSwap(
        config=TeeSwapConfig(
            chains=(ANVIL_CHAIN,),
            rpcs=(RpcConfig(chain=ANVIL_CHAIN.caip2, urls=(ANVIL_URL,)),),
            facilitators=(),
            operator=Operator(legal_name=OPERATOR_NAME, extra="MST: 0123456789"),
        ),
    )

    recipient_a = EthSigner.derive(ROOT_KEY, b"recipient-a").address
    recipient_b = EthSigner.derive(ROOT_KEY, b"recipient-b").address

    request = QuoteRequest(
        inputs=(
            TokenAmount(token=ETH, amount=Amount(ONE_ETH * 40 // 100)),
            TokenAmount(token=ETH, amount=Amount(ONE_ETH * 60 // 100)),
        ),
        outputs=(
            Balance(
                amount=TokenAmount(token=ETH, amount=Amount(ONE_ETH * 60 // 100)),
                address=Address(ANVIL_CHAIN, recipient_a),
            ),
            Balance(
                amount=TokenAmount(token=ETH, amount=Amount(ONE_ETH * 40 // 100)),
                address=Address(ANVIL_CHAIN, recipient_b),
            ),
        ),
    )

    # the quote goes over REST as JSON, so hydration is exercised the way clients hit it
    app = make_http_app(instance)
    async with AsyncTestClient(app) as client:
        resp = await client.post(
            "/teeswap/quote",
            content=encode(request),
            headers={"content-type": "application/json"},
        )
    assert resp.status_code == 200, resp.text
    quote = QuoteResponse.from_dict(decode_object(resp.content))
    quote_id = quote.quote_id
    assert quote.inputs == (TokenAmount(token=ETH, amount=Amount(ONE_ETH)),)

    accept_response = await instance.api.accept(InvoiceRequest(quote_id=quote_id))
    deposit_address = accept_response.deposits[0].address.value

    # a little over the quote: Mai carries on and the surplus stays held
    paid = ONE_ETH + SURPLUS
    await anvil.set_balance(deposit_address, paid)

    status_response = await instance.api.status(InvoiceRequest(quote_id=quote_id))
    for _ in range(30):
        await asyncio.sleep(1)
        await anvil.mine()
        status_response = await instance.api.status(InvoiceRequest(quote_id=quote_id))
        if status_response.status in ("delivered", "failed"):
            break

    assert status_response.status == "delivered", f"invoice stuck in {status_response.status}"
    (deposit,) = status_response.inputs
    assert deposit.status == InputStatus.RECEIVED
    assert deposit.deposit.address.value == deposit_address
    assert deposit.received.amount == paid
    assert [o.status for o in status_response.outputs] == [OutputStatus.DELIVERED] * 2
    assert all(len(o.transactions) == 1 for o in status_response.outputs)

    balance_a = await anvil.rpc.get_balance(recipient_a)
    balance_b = await anvil.rpc.get_balance(recipient_b)
    assert balance_a > 0
    assert balance_b > 0
    assert balance_a > balance_b

    invoice_view = await instance.api.invoice(InvoiceRequest(quote_id=quote_id))
    # nothing went wrong, so Mai did exactly what her provisional plan said
    assert tuple(a.description for a in invoice_view.actions) == quote.plan
    assert invoice_view.outputs == status_response.outputs

    # custody: every wei that arrived is held, delivered or consumed, and the chain agrees
    by_custody: dict[Custody, int] = {}
    for position in invoice_view.holdings:
        custody = position.place.custody
        by_custody[custody] = by_custody.get(custody, 0) + position.amount.amount
    assert sum(by_custody.values()) == paid
    assert by_custody.get(Custody.IN_FLIGHT, 0) == 0
    delivered = {
        p.place.address.value: p.amount.amount
        for p in invoice_view.holdings
        if p.place.custody == Custody.DELIVERED
    }
    assert delivered == {
        o.balance.address.value: o.balance.amount.amount for o in invoice_view.outputs
    }
    assert by_custody[Custody.HELD] == await anvil.rpc.get_balance(
        to_checksum_address(deposit_address)
    )
    assert by_custody[Custody.HELD] >= SURPLUS

    # --- Invoice view routes ---
    async with AsyncTestClient(app) as client:
        html_resp = await client.get(f"/invoice/{quote_id}.html")
        assert html_resp.status_code == 200
        assert "DELIVERED" in html_resp.text
        assert OPERATOR_NAME in html_resp.text
        assert "MST: 0123456789" in html_resp.text
        assert deposit_address in html_resp.text

        json_resp = await client.get(f"/invoice/{quote_id}.json")
        assert json_resp.status_code == 200
        # what a client reads back is exactly the invoice the TEE holds
        assert InvoiceView.from_dict(decode_object(json_resp.content)) == invoice_view

        not_found = await client.get("/invoice/inv_bogus.html")
        assert not_found.status_code == 404

    out = Path("dist/demo_invoice.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_resp.text)
    print(f"\n>>> invoice HTML saved to {out.resolve()}")


@pytest.mark.asyncio
async def test_usdc_split_paid_by_x402(anvil: Anvil):
    instance = TeeSwap(
        config=TeeSwapConfig(
            chains=(ANVIL_CHAIN,),
            rpcs=(RpcConfig(chain=ANVIL_CHAIN.caip2, urls=(ANVIL_URL,)),),
            facilitators=(FacilitatorConfig(url=FACILITATOR_URL),),
            operator=Operator(legal_name=OPERATOR_NAME),
        ),
    )
    instance.start_background_tasks()
    try:
        await _x402_split(instance, anvil)
    finally:
        instance.stop_background_tasks()


async def _x402_split(instance: TeeSwap, anvil: Anvil) -> None:
    for _ in range(50):
        if instance.facilitator_monitor.candidates("exact", ANVIL_CHAIN.caip2, set()):
            break
        await asyncio.sleep(0.1)
    else:
        raise RuntimeError("facilitator not healthy — run make facilitator-start")

    local = LocalChain(ANVIL_URL)
    payer = EthSigner.derive(os.urandom(32), b"payer")
    local.mint_usdc(payer.address, 10 * ONE_USDC)
    recipient_a = EthSigner.derive(os.urandom(32), b"recipient-a").address
    recipient_b = EthSigner.derive(os.urandom(32), b"recipient-b").address

    request = QuoteRequest(
        inputs=(TokenAmount(token=USDC_TOKEN, amount=Amount(5 * ONE_USDC)),),
        outputs=(
            Balance(
                amount=TokenAmount(token=USDC_TOKEN, amount=Amount(3 * ONE_USDC)),
                address=Address(ANVIL_CHAIN, recipient_a),
            ),
            Balance(
                amount=TokenAmount(token=USDC_TOKEN, amount=Amount(2 * ONE_USDC)),
                address=Address(ANVIL_CHAIN, recipient_b),
            ),
        ),
    )
    headers = {"content-type": "application/json"}
    app = make_http_app(instance)
    async with AsyncTestClient(app) as client:
        resp = await client.post("/teeswap/quote_x402", content=encode(request), headers=headers)
        assert resp.status_code == 200, resp.text
        quote = QuoteResponse.from_dict(decode_object(resp.content))
        # the facilitator pays the gas: the outputs are exactly what was asked for
        assert quote.outputs == request.outputs

        invoice_request = encode(InvoiceRequest(quote_id=quote.quote_id))
        unpaid = await client.post("/teeswap/accept_x402", content=invoice_request, headers=headers)
        assert unpaid.status_code == 402, unpaid.text
        required = PaymentRequired.from_dict(
            decode_object(base64.b64decode(unpaid.headers[HTTP_PAYMENT_REQUIRED_HEADER]))
        )
        (requirements,) = required.accepts

        # the client pays what it was asked, as any x402 client would
        now = int(Timestamp.now().dt.timestamp())
        authorization = TransferAuthorization(
            sender=payer.address,
            recipient=to_checksum_address(requirements.payTo),
            value=requirements.amount,
            valid_after=now - 60,
            valid_before=now + requirements.maxTimeoutSeconds,
            nonce=Hex32.from_bytes(os.urandom(32)),
        )
        domain = (requirements.extra["name"], requirements.extra["version"])
        signature = authorization.sign(
            payer, domain, await anvil.rpc.chain_id(), to_checksum_address(requirements.asset)
        )
        payment = PaymentPayload(
            x402Version=X402_VERSION,
            resource=required.resource,
            accepted=requirements,
            payload={"signature": "0x" + signature.hex(), "authorization": authorization.wire()},
        )
        paid = await client.post(
            "/teeswap/accept_x402",
            content=invoice_request,
            headers=headers
            | {HTTP_PAYMENT_SIGNATURE_HEADER: base64.b64encode(encode(payment)).decode()},
        )
        assert paid.status_code == 200, paid.text
        settlement = SettleResponse.from_dict(
            decode_object(base64.b64decode(paid.headers[HTTP_PAYMENT_RESPONSE_HEADER]))
        )
        assert settlement.success
        accepted = AcceptResponse.from_dict(decode_object(paid.content))
        assert settlement.transaction in accepted.instructions

        # the job runs in the app's event loop: follow it from inside the client, as a client would
        status = await _status(client, invoice_request)
        for _ in range(60):
            if status.status in ("delivered", "failed"):
                break
            await asyncio.sleep(0.5)
            status = await _status(client, invoice_request)
    assert status.status == "delivered", f"invoice stuck in {status.status}"

    (deposit,) = status.inputs
    assert deposit.status == InputStatus.RECEIVED
    assert deposit.received.amount == 5 * ONE_USDC
    assert [o.status for o in status.outputs] == [OutputStatus.DELIVERED] * 2
    assert local.usdc_balance(payer.address) == 5 * ONE_USDC
    assert local.usdc_balance(recipient_a) == 3 * ONE_USDC
    assert local.usdc_balance(recipient_b) == 2 * ONE_USDC

    # custody: everything that came in was delivered, nothing is left held or in flight
    invoice_view = await instance.api.invoice(InvoiceRequest(quote_id=quote.quote_id))
    by_custody: dict[Custody, int] = {}
    for position in invoice_view.holdings:
        custody = position.place.custody
        by_custody[custody] = by_custody.get(custody, 0) + position.amount.amount
    assert by_custody.get(Custody.DELIVERED) == 5 * ONE_USDC
    assert not by_custody.get(Custody.HELD) and not by_custody.get(Custody.IN_FLIGHT)
    assert local.usdc_balance(deposit.deposit.address.value) == 0


async def _status(client: AsyncTestClient[Litestar], invoice_request: bytes) -> StatusResponse:
    resp = await client.post(
        "/teeswap/status", content=invoice_request, headers={"content-type": "application/json"}
    )
    assert resp.status_code == 200, resp.text
    return StatusResponse.from_dict(decode_object(resp.content))
