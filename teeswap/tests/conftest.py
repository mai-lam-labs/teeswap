"""Shared fixtures for the flows that run against a local anvil node and x402 facilitator.

Requires: make anvil-start facilitator-start (make check and make coverage do both).

Every flow is written once against `api`, and runs once per interface a user can
reach TeeSwap through: in process (LocalApi), REST and MCP. TeeSwap itself is hosted
the production way, in its app, with its background tasks running.
"""

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest
import pytest_asyncio
from litestar import Litestar
from litestar.testing import AsyncTestClient

from teeswap.api import Api, McpApi, RestApi
from teeswap.app import make_http_app
from teeswap.blockchain.chains import Chain, ChainFamily
from teeswap.blockchain.evm import EthSigner
from teeswap.blockchain.rpc import RpcConfig
from teeswap.config import FacilitatorConfig, Operator, TeeSwapConfig
from teeswap.instance import TeeSwap
from teeswap.tests.localchain import USDC, LocalChain
from teeswap.types import Token
from teeswap.x402 import EvmPayer

ANVIL_URL = "http://127.0.0.1:8545"
FACILITATOR_URL = "http://127.0.0.1:18080"
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
ONE_ETH = 10**18
ONE_USDC = 10**6
OPERATOR_NAME = "CÔNG TY TNHH THỐNG TRỊ TOÀN CẦU MAI LÀM"


@pytest.fixture
def chain() -> LocalChain:
    local = LocalChain(ANVIL_URL)
    try:
        local.chain_id()
    except httpx.ConnectError as e:
        raise RuntimeError("anvil not running — run make anvil-start") from e
    return local


@dataclass(frozen=True, slots=True)
class Served:
    """TeeSwap served by its app: the instance, and an HTTP client connected to the app."""

    instance: TeeSwap
    client: AsyncTestClient[Litestar]


@pytest_asyncio.fixture
async def served(chain: LocalChain) -> AsyncIterator[Served]:
    """A TeeSwap instance served by its app, against anvil and the local facilitator."""
    instance = TeeSwap(
        config=TeeSwapConfig(
            chains=(ANVIL_CHAIN,),
            rpcs=(RpcConfig(chain=ANVIL_CHAIN.caip2, urls=(ANVIL_URL,)),),
            facilitators=(FacilitatorConfig(url=FACILITATOR_URL),),
            operator=Operator(legal_name=OPERATOR_NAME, extra="MST: 0123456789"),
        ),
    )
    # entering the client starts the app, which starts TeeSwap's background tasks
    async with AsyncTestClient(make_http_app(instance)) as client:
        yield Served(instance=instance, client=client)


@pytest.fixture(params=["local", "rest", "mcp"])
def api(request: pytest.FixtureRequest, served: Served) -> Api:
    match request.param:
        case "local":
            return served.instance.api
        case "rest":
            return RestApi(served.client)
        case _:
            return McpApi(served.client)


@pytest.fixture
def payer(chain: LocalChain) -> EvmPayer:
    """A user's wallet holding 10 USDC."""
    payer = EvmPayer(EthSigner(os.urandom(32)))
    chain.mint_usdc(payer.address, 10 * ONE_USDC)
    return payer


def new_address() -> str:
    return EthSigner(os.urandom(32)).address
