"""EVM transaction construction, signing, and submission.

Uses eth-abi for ABI encoding and eth-account for signing. No external
binaries — everything runs in-process. RPC calls use the shared jsonrpc helper.
"""

import hmac
from dataclasses import dataclass
from typing import Any, override

from eth_abi.abi import decode, encode
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_typing import BlockNumber, ChainId, ChecksumAddress, Hash32, HexStr
from eth_utils.abi import function_signature_to_4byte_selector
from eth_utils.address import to_checksum_address

from ..common import TeeSwapError
from ..http import BaseHttpClient
from ..types import Hex32, Token, Url
from .chains import Chain, ChainFamily
from .keys import ChainKey
from .rpc import jsonrpc


class EvmError(TeeSwapError):
    pass


class SigningError(EvmError):
    pass


@dataclass(frozen=True, slots=True)
class EncodedCall:
    """A call as a transaction would make it: simulated with estimate_gas, sent with send.

    A plain transfer is a call with value and empty data.
    """

    to: ChecksumAddress
    data: HexStr
    value: int = 0


@dataclass(frozen=True, slots=True)
class Receipt:
    success: bool
    gas_used: int
    effective_gas_price: int

    @property
    def gas_cost(self) -> int:
        return self.gas_used * self.effective_gas_price


@dataclass(frozen=True, slots=True)
class SignedTransaction:
    raw_tx: bytes
    tx_hash: Hash32
    sender: ChecksumAddress
    nonce: int


@dataclass(frozen=True, slots=True)
class Block:
    number: int
    timestamp: int  # seconds since the epoch, as the chain records it


# --- ABI encoding ---


def abi_encode(types: list[str], values: list[Any]) -> bytes:
    return encode(types, values)


def calldata(selector: bytes, types: list[str], values: list[Any]) -> bytes:
    return selector + encode(types, values)


def _selector(signature: str) -> bytes:
    return function_signature_to_4byte_selector(signature)


# --- Signer ---


class EthSigner(ChainKey):
    __slots__ = ("_account", "_private_key")

    def __init__(self, private_key: bytes) -> None:
        self._private_key = private_key
        self._account: LocalAccount = Account.from_key(private_key)

    @classmethod
    def derive(cls, root: bytes, label: bytes) -> EthSigner:
        child_key = hmac.digest(root, label, "sha256")
        return cls(child_key)

    @property
    @override
    def address(self) -> ChecksumAddress:
        return self._account.address

    @property
    @override
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
            sender=self.address,
            nonce=int(tx["nonce"]),
        )


@dataclass(frozen=True, slots=True)
class TransferAuthorization:
    """An EIP-3009 transferWithAuthorization: `sender` lets anyone move `value` to `recipient`."""

    sender: ChecksumAddress
    recipient: ChecksumAddress
    value: int
    valid_after: int
    valid_before: int
    nonce: Hex32

    def sign(
        self, signer: EthSigner, domain: tuple[str, str], chain_id: int, token: ChecksumAddress
    ) -> bytes:
        name, version = domain
        return signer.sign_typed_data(
            domain={
                "name": name,
                "version": version,
                "chainId": chain_id,
                "verifyingContract": token,
            },
            types={"TransferWithAuthorization": _TRANSFER_WITH_AUTHORIZATION},
            primary_type="TransferWithAuthorization",
            message={
                "from": self.sender,
                "to": self.recipient,
                "value": self.value,
                "validAfter": self.valid_after,
                "validBefore": self.valid_before,
                "nonce": self.nonce.to_bytes(),
            },
        )

    @classmethod
    def from_wire(cls, fields: dict[str, str]) -> TransferAuthorization:
        """The x402 exact-scheme `authorization` object, as wire() writes it."""
        return cls(
            sender=to_checksum_address(fields["from"]),
            recipient=to_checksum_address(fields["to"]),
            value=int(fields["value"]),
            valid_after=int(fields["validAfter"]),
            valid_before=int(fields["validBefore"]),
            nonce=Hex32(fields["nonce"]),
        )

    def wire(self) -> dict[str, str]:
        """The x402 exact-scheme `authorization` object."""
        return {
            "from": self.sender,
            "to": self.recipient,
            "value": str(self.value),
            "validAfter": str(self.valid_after),
            "validBefore": str(self.valid_before),
            "nonce": self.nonce,
        }


_TRANSFER_WITH_AUTHORIZATION = [
    {"name": "from", "type": "address"},
    {"name": "to", "type": "address"},
    {"name": "value", "type": "uint256"},
    {"name": "validAfter", "type": "uint256"},
    {"name": "validBefore", "type": "uint256"},
    {"name": "nonce", "type": "bytes32"},
]


# --- RPC client ---


class EvmRpcClient:
    def __init__(self, client: BaseHttpClient, url: Url) -> None:
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

    async def get_nonce(self, address: ChecksumAddress, block: str = "pending") -> int:
        result = await jsonrpc(self._client, self._url, "eth_getTransactionCount", [address, block])
        return int(result, 16)

    async def get_latest_block(self) -> dict[str, Any]:
        return await jsonrpc(self._client, self._url, "eth_getBlockByNumber", ["latest", False])

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


# enough to cover value + gas for any simulated call, when the sender isn't funded yet
_SIMULATED_BALANCE = hex(2**200)


class EvmChain:
    """One EVM chain: balances, gas prices, gas simulation, and sending transactions.

    Fee policy, nonce handling and signing live here and nowhere else.
    """

    def __init__(self, chain: Chain, client: BaseHttpClient, url: Url) -> None:
        if chain.family != ChainFamily.EVM:
            raise EvmError(f"{chain.name} is not an EVM chain")
        self._chain = chain
        self._client = client
        self._url = url
        self._rpc = EvmRpcClient(client, url)

    @property
    def chain(self) -> Chain:
        return self._chain

    @property
    def chain_id(self) -> int:
        # CAIP-2 for EVM is eip155:<chain id>
        return int(self._chain.caip2.split(":")[1])

    async def balance(self, address: ChecksumAddress) -> int:
        return await self._rpc.get_balance(address)

    async def token_balance(self, token: Token, owner: ChecksumAddress) -> int:
        """Native balance for the chain's native token, ERC-20 balanceOf otherwise."""
        if token.contract is None:
            return await self.balance(owner)
        data = calldata(_selector("balanceOf(address)"), ["address"], [owner])
        result = await self._rpc.call(
            to_checksum_address(token.contract), HexStr("0x" + data.hex())
        )
        (balance,) = decode(["uint256"], bytes.fromhex(result.removeprefix("0x")))
        return int(balance)

    async def authorization_used(
        self, contract: ChecksumAddress, authorizer: ChecksumAddress, nonce: Hex32
    ) -> bool:
        """EIP-3009: whether `authorizer`'s authorization `nonce` has been used or cancelled."""
        data = calldata(
            _selector("authorizationState(address,bytes32)"),
            ["address", "bytes32"],
            [authorizer, nonce.to_bytes()],
        )
        result = await self._rpc.call(contract, HexStr("0x" + data.hex()))
        (used,) = decode(["bool"], bytes.fromhex(result.removeprefix("0x")))
        return bool(used)

    async def eip712_domain(self, token: Token) -> tuple[str, str]:
        """The token's EIP-712 domain name and version, as its contract reports them."""
        if token.contract is None:
            raise EvmError(f"{token.symbol} is native: it has no EIP-712 domain")
        contract = to_checksum_address(token.contract)
        fields: list[str] = []
        for signature in ("name()", "version()"):
            result = await self._rpc.call(contract, HexStr("0x" + _selector(signature).hex()))
            (value,) = decode(["string"], bytes.fromhex(result.removeprefix("0x")))
            fields.append(str(value))
        return fields[0], fields[1]

    async def gas_price(self) -> int:
        return int(await jsonrpc(self._client, self._url, "eth_gasPrice"), 16)

    async def estimate_gas(self, sender: ChecksumAddress, call: EncodedCall) -> int:
        """Gas for this call from this sender, simulated against current state."""
        return await self._estimate(sender, call, [])

    async def estimate_gas_as_funded(self, sender: ChecksumAddress, call: EncodedCall) -> int:
        """Gas for this call as if the sender were funded; for planning before funds exist."""
        overrides = {sender: {"balance": _SIMULATED_BALANCE}}
        return await self._estimate(sender, call, ["latest", overrides])

    async def _estimate(self, sender: ChecksumAddress, call: EncodedCall, extra: list[Any]) -> int:
        tx = {"from": sender, "to": call.to, "value": hex(call.value), "data": call.data}
        return int(await jsonrpc(self._client, self._url, "eth_estimateGas", [tx, *extra]), 16)

    async def prepare(self, signer: EthSigner, call: EncodedCall) -> SignedTransaction:
        """Simulate and sign `call` from the signer's address, without sending it."""
        gas = await self.estimate_gas(signer.address, call)
        gas_price = await self.gas_price()
        nonce = await self._rpc.get_nonce(signer.address)
        return signer.sign_transaction(
            {
                "to": call.to,
                "value": call.value,
                "data": call.data,
                "gas": gas,
                "maxFeePerGas": gas_price * 2,
                "maxPriorityFeePerGas": gas_price // 10,
                "nonce": nonce,
                "chainId": self.chain_id,
            }
        )

    async def submit(self, tx: SignedTransaction) -> None:
        await self._rpc.submit_tx(tx.raw_tx)

    async def receipt(self, tx_hash: Hash32) -> Receipt | None:
        """The receipt, once the transaction is mined; None until then."""
        raw = await self._rpc.get_receipt(tx_hash)
        if raw is None:
            return None
        return Receipt(
            success=raw["status"] == "0x1",
            gas_used=int(raw["gasUsed"], 16),
            effective_gas_price=int(raw["effectiveGasPrice"], 16),
        )

    async def confirmed_nonce(self, address: ChecksumAddress) -> int:
        """How many of the address's transactions are mined: its next nonce as of the latest block."""
        return await self._rpc.get_nonce(address, "latest")

    async def latest_block(self) -> Block:
        raw = await self._rpc.get_latest_block()
        return Block(number=int(raw["number"], 16), timestamp=int(raw["timestamp"], 16))
