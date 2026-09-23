"""End-to-end engine test against a local anvil node.

Requires: make anvil-start
"""

import asyncio
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from litestar.testing import AsyncTestClient

from teeswap.app import make_http_app
from teeswap.blockchain.chains import Chain, ChainFamily
from teeswap.blockchain.evm import EthSigner, EvmRpcClient
from teeswap.blockchain.rpc import RpcConfig, jsonrpc
from teeswap.config import Operator, TeeSwapConfig
from teeswap.http import HttpClient
from teeswap.instance import TeeSwap
from teeswap.invoice import InputStatus, InvoiceView, OutputStatus
from teeswap.types import (
    Address,
    Amount,
    Balance,
    InvoiceRequest,
    QuoteRequest,
    QuoteResponse,
    Token,
    TokenAmount,
)
from teeswap.wire import decode_object, encode

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
ROOT_KEY = b"\xbb" * 32
ONE_ETH = 10**18
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

    await anvil.set_balance(deposit_address, ONE_ETH)

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
    assert deposit.received.amount >= ONE_ETH
    assert [o.status for o in status_response.outputs] == [OutputStatus.SUBMITTED] * 2
    assert all(len(o.transactions) == 1 for o in status_response.outputs)

    balance_a = await anvil.rpc.get_balance(recipient_a)
    balance_b = await anvil.rpc.get_balance(recipient_b)
    assert balance_a > 0
    assert balance_b > 0
    assert balance_a > balance_b

    invoice_view = await instance.api.invoice(InvoiceRequest(quote_id=quote_id))
    assert [a.description for a in invoice_view.actions] == [
        "wait for ETH deposit",
        "send ETH to 2 recipients",
    ]
    assert invoice_view.outputs == status_response.outputs

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
