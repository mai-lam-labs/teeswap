"""EVM transaction construction, signing, and submission.

Uses eth-abi for ABI encoding and eth-account for signing. No external
binaries — everything runs in-process. RPC calls use the shared jsonrpc helper.
"""

import hmac
from dataclasses import dataclass
from typing import Any

import httpx
from eth_abi.abi import encode
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_typing import BlockNumber, ChainId, ChecksumAddress, Hash32, HexStr

from ..common import TeeSwapError
from .rpc import jsonrpc


class EvmError(TeeSwapError):
    pass


class SigningError(EvmError):
    pass


@dataclass(frozen=True, slots=True)
class EncodedCall:
    to: ChecksumAddress
    data: HexStr
    value: int = 0


@dataclass(frozen=True, slots=True)
class SignedTransaction:
    raw_tx: bytes
    tx_hash: Hash32


# --- ABI encoding ---


def abi_encode(types: list[str], values: list[Any]) -> bytes:
    return encode(types, values)


def calldata(selector: bytes, types: list[str], values: list[Any]) -> bytes:
    return selector + encode(types, values)


# --- Signer ---


class EthSigner:
    __slots__ = ("_account", "_private_key")

    def __init__(self, private_key: bytes) -> None:
        self._private_key = private_key
        self._account: LocalAccount = Account.from_key(private_key)

    @classmethod
    def derive(cls, root: bytes, label: bytes) -> EthSigner:
        child_key = hmac.digest(root, label, "sha256")
        return cls(child_key)

    @property
    def address(self) -> ChecksumAddress:
        return self._account.address

    @property
    def private_key(self) -> bytes:
        return self._private_key

    def sign_typed_data(
        self,
        domain: dict[str, Any],
        types: dict[str, Any],
        primary_type: str,
        message: dict[str, Any],
    ) -> bytes:
        full_message = {
            "domain": domain,
            "types": types,
            "primaryType": primary_type,
            "message": message,
        }
        signed = self._account.sign_typed_data(full_message=full_message)
        return bytes(signed.signature)

    def sign_transaction(self, tx: dict[str, Any]) -> SignedTransaction:
        signed = self._account.sign_transaction(tx)
        return SignedTransaction(
            raw_tx=bytes(signed.raw_transaction),
            tx_hash=Hash32(bytes(signed.hash)),
        )


# --- RPC client ---


class EvmRpcClient:
    def __init__(self, client: httpx.AsyncClient, url: str) -> None:
        self._client = client
        self._url = url

    async def submit_tx(self, raw_tx: bytes) -> HexStr:
        hex_tx = HexStr("0x" + raw_tx.hex())
        return await jsonrpc(
            self._client, self._url, "eth_sendRawTransaction", [hex_tx], timeout=30
        )

    async def get_receipt(self, tx_hash: Hash32) -> dict[str, Any] | None:
        return await jsonrpc(
            self._client,
            self._url,
            "eth_getTransactionReceipt",
            [HexStr("0x" + tx_hash.hex())],
            timeout=30,
        )

    async def get_nonce(self, address: ChecksumAddress) -> int:
        result = await jsonrpc(
            self._client, self._url, "eth_getTransactionCount", [address, "pending"]
        )
        return int(result, 16)

    async def get_balance(self, address: ChecksumAddress) -> int:
        result = await jsonrpc(self._client, self._url, "eth_getBalance", [address, "latest"])
        return int(result, 16)

    async def call(self, to: ChecksumAddress, data: HexStr) -> HexStr:
        return await jsonrpc(
            self._client, self._url, "eth_call", [{"to": to, "data": data}, "latest"]
        )

    async def chain_id(self) -> ChainId:
        result = await jsonrpc(self._client, self._url, "eth_chainId")
        return ChainId(int(result, 16))

    async def block_number(self) -> BlockNumber:
        result = await jsonrpc(self._client, self._url, "eth_blockNumber")
        return BlockNumber(int(result, 16))
