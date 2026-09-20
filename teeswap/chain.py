import json
import subprocess
from dataclasses import dataclass
from typing import Any

import httpx

from .common import TeeSwapError
from .config import Config


class ChainError(TeeSwapError):
    pass


class RpcError(ChainError):
    pass


class TransactionError(ChainError):
    pass


class SigningError(ChainError):
    pass


@dataclass(frozen=True, slots=True)
class EncodedCall:
    to: str
    data: str
    value: str = "0"


@dataclass(frozen=True, slots=True)
class SignedTransaction:
    raw_tx: str
    tx_hash: str


def cast_encode(
    config: Config,
    signature: str,
    args: list[str],
) -> str:
    cmd = [config.cast_bin, "calldata", signature, *args]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            check=True,
            text=True,
            timeout=10,
        )
    except subprocess.CalledProcessError as e:
        raise RpcError(f"cast encode failed: {e.stderr}") from e
    return result.stdout.strip()


def cast_sign(
    config: Config,
    tx_json: dict[str, Any],
    key_fd: int,
) -> SignedTransaction:
    cmd = [
        config.cast_bin,
        "wallet",
        "sign",
        "--from-fd",
        str(key_fd),
        json.dumps(tx_json),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            check=True,
            text=True,
            timeout=10,
        )
    except subprocess.CalledProcessError as e:
        raise SigningError(f"cast sign failed: {e.stderr}") from e
    lines = result.stdout.strip().splitlines()
    return SignedTransaction(raw_tx=lines[0], tx_hash=lines[-1] if len(lines) > 1 else "")


async def submit_tx(rpc_url: str, raw_tx: str) -> str:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_sendRawTransaction",
        "params": [raw_tx],
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(rpc_url, json=payload, timeout=30)
    body = resp.json()
    if "error" in body:
        raise RpcError(f"eth_sendRawTransaction: {body['error']}")
    return body["result"]


async def get_receipt(rpc_url: str, tx_hash: str) -> dict[str, Any] | None:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_getTransactionReceipt",
        "params": [tx_hash],
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(rpc_url, json=payload, timeout=30)
    body = resp.json()
    return body.get("result")
