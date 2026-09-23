"""Set up a local anvil chain for the x402 path: contracts, and a working USDC.

`make anvil-start` runs this right after anvil comes up:

    python -m teeswap.tests.localchain http://127.0.0.1:8545

Every contract in fixtures/evm/manifest.json is installed at the address its runtime
code was fetched from on Base (anvil_setCode): the three contracts the x402
facilitator requires at startup, USDC's FiatTokenV2_2 implementation, and the
SignatureChecker library that implementation is linked against (without it every
signature check reverts with no data).

setCode gives code without storage, so USDC is then initialised through its own
initialize* functions. Balances are left to the tests: mint_usdc().

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

FIXTURES = Path(__file__).parent / "fixtures" / "evm"
RECEIPT_POLL_SECONDS = 0.1
RECEIPT_ATTEMPTS = 50


@dataclass(frozen=True, slots=True)
class LocalUsdc:
    address: str
    name: str = "USD Coin"
    symbol: str = "USDC"
    version: str = "2"
    decimals: int = 6


def _manifest() -> dict[str, Any]:
    with (FIXTURES / "manifest.json").open() as f:
        return json.load(f)


USDC = LocalUsdc(address=_manifest()["contracts"]["usdc_fiat_token_v2_2"]["address"])


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
        for _ in range(RECEIPT_ATTEMPTS):
            receipt = self._rpc("eth_getTransactionReceipt", [tx_hash])
            if receipt is not None:
                if receipt["status"] != "0x1":
                    raise LocalChainError(f"{signature} reverted")
                return
            time.sleep(RECEIPT_POLL_SECONDS)
        raise LocalChainError(f"{signature}: no receipt for {tx_hash}")

    @property
    def usdc_owner(self) -> str:
        """anvil's second dev account: USDC owner, master minter, pauser and blacklister."""
        return self._rpc("eth_accounts", [])[1]

    def install_contracts(self) -> None:
        for name, contract in _manifest()["contracts"].items():
            code = (FIXTURES / f"{name}.hex").read_text().strip()
            self._rpc("anvil_setCode", [contract["address"], code])

    def initialize_usdc(self) -> None:
        master_minter = self.call(USDC.address, "masterMinter()", [], [])
        if int.from_bytes(master_minter) != 0:
            return
        owner = self.usdc_owner
        steps: list[tuple[str, list[str], list[Any]]] = [
            (
                "initialize(string,string,string,uint8,address,address,address,address)",
                ["string", "string", "string", "uint8"] + ["address"] * 4,
                [USDC.name, USDC.symbol, "USD", USDC.decimals, owner, owner, owner, owner],
            ),
            ("initializeV2(string)", ["string"], [USDC.name]),
            ("initializeV2_1(address)", ["address"], [owner]),
            ("initializeV2_2(address[],string)", ["address[]", "string"], [[], USDC.symbol]),
            ("configureMinter(address,uint256)", ["address", "uint256"], [owner, 2**255]),
        ]
        for signature, types, args in steps:
            self.send(owner, USDC.address, signature, types, args)

    def setup(self) -> None:
        self.install_contracts()
        self.initialize_usdc()

    def mint_usdc(self, to: str, amount: int) -> None:
        self.send(
            self.usdc_owner,
            USDC.address,
            "mint(address,uint256)",
            ["address", "uint256"],
            [to, amount],
        )

    def usdc_balance(self, account: str) -> int:
        return int.from_bytes(self.call(USDC.address, "balanceOf(address)", ["address"], [account]))


if __name__ == "__main__":
    LocalChain(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8545").setup()
    print(f">> local chain ready: facilitator contracts + USDC at {USDC.address}")
