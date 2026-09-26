"""Shared fixtures for the flows that run against a local anvil node and x402 facilitator.

Requires: make anvil-start facilitator-start (make check and make coverage do both).

Every flow is written once against `api`, and runs once per interface a user can
reach TeeSwap through: in process (LocalApi), REST and MCP. TeeSwap itself is hosted
the production way, in its app, with its background tasks running.
"""

import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from eth_typing import ChecksumAddress
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


# each flow's invoice page, as the app serves it, kept for reference after the run
INVOICE_PAGES = Path("dist/test-invoices")

type SaveInvoice = Callable[[str], Awaitable[str]]


@pytest.fixture
def invoice_page(request: pytest.FixtureRequest, served: Served) -> SaveInvoice:
    """Fetch an invoice's page from the app and save it as dist/test-invoices/<test>.html."""

    async def save(quote_id: str) -> str:
        page = await served.client.get(f"/invoice/{quote_id}.html")
        assert page.status_code == 200, page.text
        INVOICE_PAGES.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^\w.-]+", "_", request.node.name).strip("_")
        (INVOICE_PAGES / f"{name}.html").write_text(page.text)
        return page.text

    return save


@pytest.fixture
def payer(chain: LocalChain) -> EvmPayer:
    """A user's wallet holding 10 USDC."""
    payer = EvmPayer(EthSigner(os.urandom(32)))
    chain.mint(USDC, payer.address, 10 * ONE_USDC)
    return payer


def new_address() -> ChecksumAddress:
    return EthSigner(os.urandom(32)).address


GAS_SPIKE = 100  # times the base fee a quote was made at


@pytest.fixture
def gas_spike(chain: LocalChain) -> Iterator[Callable[[], None]]:
    """Makes gas cost GAS_SPIKE times more from the next block; puts it back afterwards,
    since every test shares the chain."""
    before = chain.base_fee()

    def spike() -> None:
        chain.set_base_fee(before * GAS_SPIKE)

    yield spike
    chain.set_base_fee(before)


@dataclass(frozen=True, slots=True)
class FacilitatorGas:
    """The local facilitator's gas money: take it away, give it back."""

    chain: LocalChain
    balance: int

    def starve(self) -> None:
        self.chain.set_eth_balance(self.chain.facilitator_account, 0)

    def feed(self) -> None:
        self.chain.set_eth_balance(self.chain.facilitator_account, self.balance)


@pytest.fixture
def facilitator_gas(chain: LocalChain) -> Iterator[FacilitatorGas]:
    """The facilitator's gas; given back afterwards, since every test shares the chain."""
    gas = FacilitatorGas(chain, chain.eth_balance(chain.facilitator_account))
    yield gas
    gas.feed()


@pytest.fixture
def block_time(chain: LocalChain) -> Iterator[None]:
    """A chain whose time moves on: a block every second, as a real chain makes them.
    anvil otherwise only mines for a transaction, so waiting on chain time waits forever."""
    chain.mine_every(1)
    yield
    chain.mine_on_demand()
