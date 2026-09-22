"""CoW Protocol integration — same-chain EVM swaps via the solver auction.

Orders are EIP-712 signed off-chain and submitted to the CoW orderbook API.
Solvers compete to fill. The TEE never pays gas for the order — solvers handle
settlement. The per-invoice address needs gas only for the ERC-20 approval to
GPv2VaultRelayer.

Flow: approve VaultRelayer (on-chain, needs gas) → quote (HTTP) →
      sign order (EIP-712) → submit (HTTP) → poll (HTTP)

Docs: https://docs.cow.fi
API:  https://api.cow.fi
Contracts: https://github.com/cowprotocol/contracts
"""

import enum
from dataclasses import dataclass
from typing import override

import httpx
from eth_typing import ChecksumAddress, HexAddress, HexStr

from ..blockchain.chains import Chain
from ..blockchain.evm import EthSigner, EvmRpcClient, calldata
from ..protocol import OrderState, OrderStatus, Protocol, ProtocolMeta
from ..types import ProtocolClass

GPV2_SETTLEMENT = ChecksumAddress(HexAddress(HexStr("0x9008D19f58AAbD9eD0D60971565AA8510560ab41")))
GPV2_VAULT_RELAYER = ChecksumAddress(
    HexAddress(HexStr("0xC92E8bdf79f0507f65a392b0ab4667716BFE0110"))
)

EIP712_DOMAIN = {
    "name": "Gnosis Protocol",
    "version": "v2",
}

EIP712_ORDER_TYPES = {
    "Order": [
        {"name": "sellToken", "type": "address"},
        {"name": "buyToken", "type": "address"},
        {"name": "receiver", "type": "address"},
        {"name": "sellAmount", "type": "uint256"},
        {"name": "buyAmount", "type": "uint256"},
        {"name": "validTo", "type": "uint32"},
        {"name": "appData", "type": "bytes32"},
        {"name": "feeAmount", "type": "uint256"},
        {"name": "kind", "type": "string"},
        {"name": "partiallyFillable", "type": "bool"},
        {"name": "sellTokenBalance", "type": "string"},
        {"name": "buyTokenBalance", "type": "string"},
    ],
}

APP_DATA_HASH = "0xb48d38f93eaa084033fc5970bf96e559c33c4cdc07d889ab00b4d63f9590739d"

# ERC-20 approve(address,uint256) selector
_APPROVE_SELECTOR = bytes.fromhex("095ea7b3")
_MAX_UINT256 = 2**256 - 1

_API_URLS: dict[int, str] = {
    1: "https://api.cow.fi/mainnet",
    56: "https://api.cow.fi/bnb",
    100: "https://api.cow.fi/xdai",
    137: "https://api.cow.fi/polygon",
    8453: "https://api.cow.fi/base",
    42161: "https://api.cow.fi/arbitrum_one",
    43114: "https://api.cow.fi/avalanche",
    59144: "https://api.cow.fi/linea",
    11155111: "https://api.cow.fi/sepolia",
}

_COW_STATUS_MAP: dict[str, OrderStatus] = {
    "presignaturePending": OrderStatus.PENDING,
    "open": OrderStatus.OPEN,
    "fulfilled": OrderStatus.FULFILLED,
    "cancelled": OrderStatus.CANCELLED,
    "expired": OrderStatus.EXPIRED,
}


class OrderKind(enum.StrEnum):
    SELL = "sell"
    BUY = "buy"


@dataclass(frozen=True, slots=True)
class CowOrder:
    sell_token: ChecksumAddress
    buy_token: ChecksumAddress
    receiver: ChecksumAddress
    sell_amount: int
    buy_amount: int
    valid_to: int
    app_data: str
    fee_amount: int
    kind: OrderKind
    partially_fillable: bool = False


@dataclass(frozen=True, slots=True)
class CowQuote:
    order: CowOrder
    quote_id: int
    chain: Chain


def _chain_id(chain: Chain) -> int:
    return int(chain.caip2.split(":")[1])


def _api_url(chain: Chain) -> str:
    cid = _chain_id(chain)
    url = _API_URLS.get(cid)
    if url is None:
        raise ValueError(f"CoW Protocol not available on {chain.name} ({chain.caip2})")
    return url


def _domain_for_chain(chain: Chain) -> dict[str, str | int]:
    return {
        **EIP712_DOMAIN,
        "chainId": _chain_id(chain),
        "verifyingContract": GPV2_SETTLEMENT,
    }


def _order_to_eip712(order: CowOrder) -> dict[str, str | int | bool]:
    return {
        "sellToken": order.sell_token,
        "buyToken": order.buy_token,
        "receiver": order.receiver,
        "sellAmount": order.sell_amount,
        "buyAmount": order.buy_amount,
        "validTo": order.valid_to,
        "appData": order.app_data,
        "feeAmount": order.fee_amount,
        "kind": order.kind.value,
        "partiallyFillable": order.partially_fillable,
        "sellTokenBalance": "erc20",
        "buyTokenBalance": "erc20",
    }


def sign_order(signer: EthSigner, order: CowOrder, chain: Chain) -> bytes:
    return signer.sign_typed_data(
        domain=_domain_for_chain(chain),
        types=EIP712_ORDER_TYPES,
        primary_type="Order",
        message=_order_to_eip712(order),
    )


async def approve_vault_relayer(
    signer: EthSigner,
    rpc: EvmRpcClient,
    token: ChecksumAddress,
    chain: Chain,
) -> str:
    data = calldata(_APPROVE_SELECTOR, ["address", "uint256"], [GPV2_VAULT_RELAYER, _MAX_UINT256])
    nonce = await rpc.get_nonce(signer.address)
    tx = {
        "to": token,
        "data": data,
        "nonce": nonce,
        "chainId": _chain_id(chain),
        "gas": 60_000,
        "maxFeePerGas": 2_000_000_000,
        "maxPriorityFeePerGas": 100_000_000,
    }
    signed = signer.sign_transaction(tx)
    return await rpc.submit_tx(signed.raw_tx)


class CowProtocol(Protocol):
    def __init__(
        self,
        supported_chains: tuple[Chain, ...],
        client: httpx.AsyncClient,
    ) -> None:
        self._chains = supported_chains
        self._client = client

    @property
    @override
    def meta(self) -> ProtocolMeta:
        return ProtocolMeta(
            name="cow",
            protocol_class=ProtocolClass.A,
            cross_chain=False,
            supported_chains=self._chains,
        )

    async def quote(
        self,
        signer: EthSigner,
        sell_token: ChecksumAddress,
        buy_token: ChecksumAddress,
        sell_amount: int,
        chain: Chain,
        receiver: ChecksumAddress | None = None,
        kind: OrderKind = OrderKind.SELL,
    ) -> CowQuote:
        base = _api_url(chain)
        body = {
            "sellToken": sell_token,
            "buyToken": buy_token,
            "sellAmountBeforeFee": str(sell_amount),
            "from": signer.address,
            "kind": kind.value,
            "priceQuality": "optimal",
            "signingScheme": "eip712",
        }
        if receiver is not None:
            body["receiver"] = receiver
        resp = await self._client.post(f"{base}/api/v1/quote", json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        q = data["quote"]
        order = CowOrder(
            sell_token=ChecksumAddress(q["sellToken"]),
            buy_token=ChecksumAddress(q["buyToken"]),
            receiver=ChecksumAddress(q.get("receiver") or receiver or signer.address),
            sell_amount=int(q["sellAmount"]),
            buy_amount=int(q["buyAmount"]),
            valid_to=q["validTo"],
            app_data=q.get("appData", APP_DATA_HASH),
            fee_amount=int(q.get("feeAmount", "0")),
            kind=OrderKind(q.get("kind", kind.value)),
        )
        return CowQuote(order=order, quote_id=data["id"], chain=chain)

    async def execute(self, signer: EthSigner, cow_quote: CowQuote) -> str:
        signature = sign_order(signer, cow_quote.order, cow_quote.chain)
        base = _api_url(cow_quote.chain)
        order = cow_quote.order
        body = {
            "sellToken": order.sell_token,
            "buyToken": order.buy_token,
            "receiver": order.receiver,
            "sellAmount": str(order.sell_amount),
            "buyAmount": str(order.buy_amount),
            "validTo": order.valid_to,
            "appData": order.app_data,
            "feeAmount": str(order.fee_amount),
            "kind": order.kind.value,
            "partiallyFillable": order.partially_fillable,
            "sellTokenBalance": "erc20",
            "buyTokenBalance": "erc20",
            "signingScheme": "eip712",
            "signature": "0x" + signature.hex(),
            "from": signer.address,
            "quoteId": cow_quote.quote_id,
        }
        resp = await self._client.post(f"{base}/api/v1/orders", json=body, timeout=30)
        resp.raise_for_status()
        return resp.json()

    @override
    async def poll(self, order_id: str, chain: Chain) -> OrderState:
        base = _api_url(chain)
        resp = await self._client.get(f"{base}/api/v1/orders/{order_id}", timeout=30)
        resp.raise_for_status()
        data = resp.json()
        status = _COW_STATUS_MAP.get(data["status"], OrderStatus.PENDING)
        return OrderState(
            order_id=order_id,
            status=status,
            executed_sell=int(data["executedSellAmount"]) or None,
            executed_buy=int(data["executedBuyAmount"]) or None,
            tx_hash=None,
        )
