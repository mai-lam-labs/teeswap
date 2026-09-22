"""End-to-end engine test against a local anvil node.

Requires: make anvil-start
"""

import asyncio

import httpx
import pytest
import pytest_asyncio

from teeswap.blockchain.chains import Chain, ChainFamily
from teeswap.blockchain.evm import EthSigner, EvmRpcClient
from teeswap.blockchain.rpc import RpcConfig, jsonrpc
from teeswap.config import TeeSwapConfig
from teeswap.http import HttpClient
from teeswap.instance import TeeSwap
from teeswap.types import AcceptRequest, Output, QuoteRequest, StatusRequest, Token, TokenAmount

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
    except httpx.ConnectError:
        pytest.fail("anvil not running — run make anvil-start")
    yield node
    await node.close()


@pytest.mark.asyncio
async def test_eth_split_via_api(anvil: Anvil):
    instance = TeeSwap(
        config=TeeSwapConfig(
            chains=(ANVIL_CHAIN,),
            rpcs=(RpcConfig(chain=ANVIL_CHAIN.caip2, urls=(ANVIL_URL,)),),
            facilitators=(),
        ),
    )

    recipient_a = EthSigner.derive(ROOT_KEY, b"recipient-a").address
    recipient_b = EthSigner.derive(ROOT_KEY, b"recipient-b").address

    quote_response = await instance.api.quote(
        QuoteRequest(
            input=TokenAmount(token=ETH, amount=ONE_ETH),
            outputs=(
                Output(token=ETH, amount=ONE_ETH * 60 // 100, recipient=recipient_a),
                Output(token=ETH, amount=ONE_ETH * 40 // 100, recipient=recipient_b),
            ),
        )
    )
    quote_id = quote_response.quote_id

    accept_response = await instance.api.accept(AcceptRequest(quote_id=quote_id))
    deposit_address = accept_response.deposits[0].address

    await anvil.set_balance(deposit_address, ONE_ETH)

    status_response = await instance.api.status(StatusRequest(quote_id=quote_id))
    for _ in range(30):
        await asyncio.sleep(1)
        await anvil.mine()
        status_response = await instance.api.status(StatusRequest(quote_id=quote_id))
        if status_response.status in ("delivered", "failed"):
            break

    assert status_response.status == "delivered", f"invoice stuck in {status_response.status}"
    assert status_response.deposit_address == deposit_address
    assert len(status_response.actions) == 2

    balance_a = await anvil.rpc.get_balance(recipient_a)
    balance_b = await anvil.rpc.get_balance(recipient_b)
    assert balance_a > 0
    assert balance_b > 0
    assert balance_a > balance_b
