"""Set up a local anvil chain for the x402 path: contracts, a working USDC, and DORP.

`make anvil-start` runs this right after anvil comes up:

    python -m teeswap.tests.localchain http://127.0.0.1:8545

Every contract in fixtures/evm/manifest.json is installed at the address its runtime
code was fetched from on Base (anvil_setCode): the three contracts the x402
facilitator requires at startup, USDC's FiatTokenV2_2 implementation, and the
SignatureChecker library that implementation is linked against (without it every
signature check reverts with no data).

setCode gives code without storage, so USDC is then initialised through its own
initialize* functions. DORP is the same contract at another address, initialised as a
token of its own: a real EIP-3009 token that nobody asked for. Balances are left to
the tests: mint().

Idempotent: running it on an already set-up chain changes nothing.
"""

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from eth_abi.abi import encode
from eth_utils.abi import function_signature_to_4byte_selector
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

FIXTURES = Path(__file__).parent / "fixtures" / "evm"
RECEIPT_POLL_SECONDS = 0.1
RECEIPT_ATTEMPTS = 50
# anvil's dev accounts: #0 pays the facilitator's gas, #1 owns the tokens, #2 plays a
# user's wallet
FACILITATOR_ACCOUNT = 0
DEPOSITOR_ACCOUNT = 2
# PUSH1 0 PUSH1 0 REVERT: code that refuses every call and every payment
REVERT_ALWAYS = "0x60006000fd"


@dataclass(frozen=True, slots=True)
class LocalToken:
    """A FiatToken (USDC's contract) on the local chain."""

    address: str
    name: str
    symbol: str
    version: str = "2"
    decimals: int = 6


def _manifest() -> dict[str, Any]:
    with (FIXTURES / "manifest.json").open() as f:
        return json.load(f)


USDC_CONTRACT = "usdc_fiat_token_v2_2"
USDC = LocalToken(
    address=_manifest()["contracts"][USDC_CONTRACT]["address"], name="USD Coin", symbol="USDC"
)
DORP = LocalToken(
    address=to_checksum_address(keccak(text="DORP")[-20:]), name="Dorp", symbol="DORP"
)
TOKENS = (USDC, DORP)


class LocalChainError(Exception):
    pass


class LocalChain:
    def __init__(self, rpc_url: str) -> None:
        self._rpc_url = rpc_url

    def _rpc(self, method: str, params: list[Any]) -> Any:
        body = httpx.post(
            self._rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=10,
        ).json()
        if "error" in body:
            raise LocalChainError(f"{method}: {body['error']}")
        return body["result"]

    def _calldata(self, signature: str, types: list[str], args: list[Any]) -> str:
        return "0x" + (function_signature_to_4byte_selector(signature) + encode(types, args)).hex()

    def call(self, to: str, signature: str, types: list[str], args: list[Any]) -> bytes:
        data = self._calldata(signature, types, args)
        return bytes.fromhex(self._rpc("eth_call", [{"to": to, "data": data}, "latest"])[2:])

    def send(self, sender: str, to: str, signature: str, types: list[str], args: list[Any]) -> None:
        """Send from an unlocked anvil account and wait for a successful receipt."""
        data = self._calldata(signature, types, args)
        tx_hash = self._rpc("eth_sendTransaction", [{"from": sender, "to": to, "data": data}])
        self._wait(tx_hash, signature)

    def _wait(self, tx_hash: str, what: str) -> None:
        for _ in range(RECEIPT_ATTEMPTS):
            receipt = self._rpc("eth_getTransactionReceipt", [tx_hash])
            if receipt is not None:
                if receipt["status"] != "0x1":
                    raise LocalChainError(f"{what} reverted")
                return
            time.sleep(RECEIPT_POLL_SECONDS)
        raise LocalChainError(f"{what}: no receipt for {tx_hash}")

    def chain_id(self) -> int:
        return int(self._rpc("eth_chainId", []), 16)

    @property
    def token_owner(self) -> str:
        """anvil's second dev account: every token's owner, master minter, pauser and
        blacklister."""
        return self._rpc("eth_accounts", [])[1]

    def install_contracts(self) -> None:
        for name, contract in _manifest()["contracts"].items():
            self._rpc("anvil_setCode", [contract["address"], _code(name)])
        self._rpc("anvil_setCode", [DORP.address, _code(USDC_CONTRACT)])

    def initialize_token(self, token: LocalToken) -> None:
        master_minter = self.call(token.address, "masterMinter()", [], [])
        if int.from_bytes(master_minter) != 0:
            return
        owner = self.token_owner
        steps: list[tuple[str, list[str], list[Any]]] = [
            (
                "initialize(string,string,string,uint8,address,address,address,address)",
                ["string", "string", "string", "uint8"] + ["address"] * 4,
                [token.name, token.symbol, "USD", token.decimals, owner, owner, owner, owner],
            ),
            ("initializeV2(string)", ["string"], [token.name]),
            ("initializeV2_1(address)", ["address"], [owner]),
            ("initializeV2_2(address[],string)", ["address[]", "string"], [[], token.symbol]),
            ("configureMinter(address,uint256)", ["address", "uint256"], [owner, 2**255]),
        ]
        for signature, types, args in steps:
            self.send(owner, token.address, signature, types, args)

    def setup(self) -> None:
        self.install_contracts()
        for token in TOKENS:
            self.initialize_token(token)

    def mint(self, token: LocalToken, to: str, amount: int) -> None:
        self.send(
            self.token_owner,
            token.address,
            "mint(address,uint256)",
            ["address", "uint256"],
            [to, amount],
        )

    def transfer_eth(self, to: str, amount: int) -> None:
        """Send ETH as a user's wallet would: a mined transfer from a funded dev account."""
        sender = self._rpc("eth_accounts", [])[DEPOSITOR_ACCOUNT]
        tx_hash = self._rpc(
            "eth_sendTransaction", [{"from": sender, "to": to, "value": hex(amount)}]
        )
        self._wait(tx_hash, "ETH transfer")

    def refuse_payments(self, account: str) -> None:
        """Make `account` a contract that reverts whatever is sent to it."""
        self._rpc("anvil_setCode", [account, REVERT_ALWAYS])

    def base_fee(self) -> int:
        return int(self._rpc("eth_getBlockByNumber", ["latest", False])["baseFeePerGas"], 16)

    def set_base_fee(self, wei: int) -> None:
        """Set the base fee from the next block on (EIP-1559 then moves it block by block)."""
        self._rpc("anvil_setNextBlockBaseFeePerGas", [hex(wei)])
        self._rpc("evm_mine", [])

    @property
    def facilitator_account(self) -> str:
        """The account the local facilitator pays settlement gas from."""
        return self._rpc("eth_accounts", [])[FACILITATOR_ACCOUNT]

    def mine_every(self, seconds: int) -> None:
        """Make a block every `seconds`, as a real chain does, transactions or not."""
        self._rpc("evm_setIntervalMining", [seconds])

    def mine_on_demand(self) -> None:
        """anvil's default: a block for each transaction, and none otherwise."""
        self._rpc("evm_setIntervalMining", [0])
        self._rpc("evm_setAutomine", [True])

    def set_eth_balance(self, account: str, wei: int) -> None:
        self._rpc("anvil_setBalance", [account, hex(wei)])

    def eth_balance(self, account: str) -> int:
        return int(self._rpc("eth_getBalance", [account, "latest"]), 16)

    def balance_of(self, token: LocalToken, account: str) -> int:
        return int.from_bytes(
            self.call(token.address, "balanceOf(address)", ["address"], [account])
        )


def _code(contract: str) -> str:
    return (FIXTURES / f"{contract}.hex").read_text().strip()


if __name__ == "__main__":
    LocalChain(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8545").setup()
    tokens = ", ".join(f"{t.symbol} at {t.address}" for t in TOKENS)
    print(f">> local chain ready: facilitator contracts, {tokens}")
