"""RPC endpoint monitor — polls configured RPCs for liveness, latency, and block height.

Each RPC is checked with a chain-family-specific health check that verifies:
- The endpoint responds (liveness)
- The chain ID matches what we expect (misconfiguration detection)
- The current block height (stale node detection)
- Round-trip latency
"""

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..http import BaseHttpClient, HttpClient
from ..types import SecureUrl
from .chains import Chain, ChainFamily, ChainRegistry

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

REQUEST_TIMEOUT = 10.0
DEFAULT_POLL_INTERVAL = 300.0
DEFAULT_CONCURRENCY = 6


@dataclass(frozen=True, slots=True)
class RpcConfig:
    chain: str
    urls: tuple[SecureUrl | str, ...]


@dataclass(frozen=True, slots=True)
class RpcStatus:
    url: str
    chain: Chain
    healthy: bool
    chain_id_match: bool | None
    block_height: int | None
    latency_ms: float | None
    error: str | None
    last_polled: float


@dataclass(slots=True)
class RpcSnapshot:
    endpoints: dict[str, RpcStatus] = field(default_factory=dict)
    updated_at: float = 0.0

    def for_chain(self, chain: Chain) -> list[RpcStatus]:
        return [s for s in self.endpoints.values() if s.chain is chain and s.healthy]

    def best_for_chain(self, chain: Chain) -> RpcStatus | None:
        healthy = self.for_chain(chain)
        if not healthy:
            return None
        return min(healthy, key=lambda s: s.latency_ms or float("inf"))


# --- JSON-RPC helper ---


async def jsonrpc(
    client: BaseHttpClient,
    url: SecureUrl | str,
    method: str,
    params: list[Any] | None = None,
    timeout: float = REQUEST_TIMEOUT,
) -> Any:
    resp = await client.post(
        url,
        json={"jsonrpc": "2.0", "method": method, "params": params or [], "id": 1},
        timeout=timeout,
    )
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        raise JsonRpcError(body["error"])
    return body.get("result")


class JsonRpcError(Exception):
    def __init__(self, error: dict[str, Any]) -> None:
        self.code: int = error.get("code", 0)
        self.rpc_message: str = error.get("message", "")
        super().__init__(f"JSON-RPC {self.code}: {self.rpc_message}")


async def _rest_get(
    client: BaseHttpClient,
    url: SecureUrl | str,
) -> Any:
    resp = await client.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


async def _rest_post(
    client: BaseHttpClient,
    url: SecureUrl | str,
    body: Any = None,
) -> Any:
    resp = await client.post(url, json=body or {}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


# --- Check result ---


@dataclass(frozen=True, slots=True)
class _CheckResult:
    healthy: bool
    chain_id_match: bool | None
    block_height: int | None
    error: str | None = None


# --- Per-family probes ---


def _parse_hex_or_int(value: Any) -> int:
    if isinstance(value, int):
        return value
    s = str(value)
    return int(s, 16) if s.startswith("0x") else int(s)


async def _probe_evm(client: BaseHttpClient, url: SecureUrl | str, chain: Chain) -> _CheckResult:
    chain_id_raw = await jsonrpc(client, url, "eth_chainId")
    block_raw = await jsonrpc(client, url, "eth_blockNumber")

    expected_id = chain.caip2.split(":")[1]
    actual_id = str(_parse_hex_or_int(chain_id_raw))
    match = actual_id == expected_id

    if not match:
        logger.warning(
            "rpc %s: chain ID mismatch (expected %s, got %s)",
            url,
            expected_id,
            actual_id,
        )

    return _CheckResult(
        healthy=True,
        chain_id_match=match,
        block_height=_parse_hex_or_int(block_raw),
    )


async def _probe_svm(client: BaseHttpClient, url: SecureUrl | str, chain: Chain) -> _CheckResult:
    health = await jsonrpc(client, url, "getHealth")
    slot = await jsonrpc(client, url, "getSlot")
    genesis = await jsonrpc(client, url, "getGenesisHash")
    ok = health == "ok"
    expected_prefix = chain.caip2.split(":")[1]
    match = genesis.startswith(expected_prefix)
    return _CheckResult(
        healthy=ok,
        chain_id_match=match,
        block_height=slot,
        error=None if ok else f"health: {health}",
    )


async def _probe_stellar(
    client: BaseHttpClient, url: SecureUrl | str, chain: Chain
) -> _CheckResult:
    data = await _rest_get(client, url)
    expected = chain.caip2.split(":")[1]
    passphrase = data.get("network_passphrase", "")
    match = (expected == "pubnet" and "Public" in passphrase) or (
        expected == "testnet" and "Test" in passphrase
    )
    return _CheckResult(
        healthy=True,
        chain_id_match=match,
        block_height=data.get("history_latest_ledger", 0),
    )


async def _probe_algorand(
    client: BaseHttpClient, url: SecureUrl | str, chain: Chain
) -> _CheckResult:
    data = await _rest_get(client, f"{url}/v2/status")
    genesis_hash = data.get("genesis-hash", "")
    expected_hash = chain.caip2.split(":")[1]
    match = genesis_hash.startswith(expected_hash)
    return _CheckResult(
        healthy=True,
        chain_id_match=match,
        block_height=data.get("last-round", 0),
    )


async def _probe_near(client: BaseHttpClient, url: SecureUrl | str, chain: Chain) -> _CheckResult:
    result = await jsonrpc(client, url, "status")
    actual_chain_id = result.get("chain_id", "")
    expected = chain.caip2.split(":")[1]
    match = actual_chain_id == expected
    block = result.get("sync_info", {}).get("latest_block_height", 0)
    return _CheckResult(healthy=True, chain_id_match=match, block_height=block)


async def _probe_sui(client: BaseHttpClient, url: SecureUrl | str, chain: Chain) -> _CheckResult:
    checkpoint = await jsonrpc(client, url, "sui_getLatestCheckpointSequenceNumber")
    chain_id = await jsonrpc(client, url, "sui_getChainIdentifier")
    expected = chain.caip2.split(":")[1]
    match = chain_id is not None and chain_id == expected
    return _CheckResult(
        healthy=True,
        chain_id_match=match,
        block_height=int(checkpoint) if checkpoint is not None else None,
    )


async def _probe_xrpl(client: BaseHttpClient, url: SecureUrl | str, chain: Chain) -> _CheckResult:
    data = await _rest_post(client, url, {"method": "server_info", "params": [{}]})
    info = data.get("result", {}).get("info", {})
    net_id = str(info.get("network_id", ""))
    expected = chain.caip2.split(":")[1]
    match = net_id == expected
    ledger = info.get("validated_ledger", {}).get("seq", 0)
    return _CheckResult(healthy=True, chain_id_match=match, block_height=ledger)


async def _probe_tron(client: BaseHttpClient, url: SecureUrl | str, chain: Chain) -> _CheckResult:
    chain_id_raw = await jsonrpc(client, url, "eth_chainId")
    block_raw = await jsonrpc(client, url, "eth_blockNumber")
    expected_id = chain.caip2.split(":")[1]
    actual_id = hex(_parse_hex_or_int(chain_id_raw))
    match = actual_id == expected_id
    return _CheckResult(
        healthy=True,
        chain_id_match=match,
        block_height=_parse_hex_or_int(block_raw),
    )


async def _probe_aptos(client: BaseHttpClient, url: SecureUrl | str, chain: Chain) -> _CheckResult:
    data = await _rest_get(client, f"{url}/v1")
    actual_chain_id = str(data.get("chain_id", ""))
    expected = chain.caip2.split(":")[1]
    match = actual_chain_id == expected
    block = int(data.get("block_height", "0"))
    return _CheckResult(healthy=True, chain_id_match=match, block_height=block)


type Probe = Callable[[BaseHttpClient, SecureUrl | str, Chain], Coroutine[Any, Any, _CheckResult]]

_PROBES: dict[ChainFamily, Probe] = {
    ChainFamily.EVM: _probe_evm,
    ChainFamily.SVM: _probe_svm,
    ChainFamily.STELLAR: _probe_stellar,
    ChainFamily.ALGORAND: _probe_algorand,
    ChainFamily.NEAR: _probe_near,
    ChainFamily.SUI: _probe_sui,
    ChainFamily.XRPL: _probe_xrpl,
    ChainFamily.TRON: _probe_tron,
    ChainFamily.APTOS: _probe_aptos,
}


# --- Unified check ---


def _url_str(url: SecureUrl | str) -> str:
    return str(url)


async def check_rpc(
    client: BaseHttpClient,
    url: SecureUrl | str,
    chain: Chain,
) -> RpcStatus:
    now = time.time()
    probe = _PROBES.get(chain.family)
    if probe is None:
        return RpcStatus(
            url=_url_str(url),
            chain=chain,
            healthy=False,
            chain_id_match=None,
            block_height=None,
            latency_ms=None,
            error=f"no probe for {chain.family}",
            last_polled=now,
        )
    try:
        start = time.monotonic()
        result = await probe(client, url, chain)
        latency = (time.monotonic() - start) * 1000
        return RpcStatus(
            url=_url_str(url),
            chain=chain,
            healthy=result.healthy,
            chain_id_match=result.chain_id_match,
            block_height=result.block_height,
            latency_ms=round(latency, 1),
            error=result.error,
            last_polled=now,
        )
    except Exception as e:  # noqa: BLE001
        return RpcStatus(
            url=_url_str(url),
            chain=chain,
            healthy=False,
            chain_id_match=None,
            block_height=None,
            latency_ms=None,
            error=_classify_error(e),
            last_polled=now,
        )


def _classify_error(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        msg = str(exc)
        if "Name or service not known" in msg or "getaddrinfo failed" in msg:
            return "dns failure"
        if "Connection refused" in msg:
            return "connection refused"
        if "SSL" in msg or "certificate" in msg.lower():
            return f"tls error: {msg[:80]}"
        return f"connect error: {msg[:80]}"
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        label = {429: "rate limited", 401: "unauthorized", 403: "forbidden"}.get(code)
        return label or f"http {code}"
    if isinstance(exc, httpx.HTTPError):
        return f"http error: {str(exc)[:80]}"
    return f"{type(exc).__name__}: {str(exc)[:80]}"


# --- Monitor ---


class RpcMonitor:
    def __init__(
        self,
        configs: tuple[RpcConfig, ...],
        registry: ChainRegistry,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        self._configs = configs
        self._registry = registry
        self._poll_interval = poll_interval
        self._semaphore = asyncio.Semaphore(concurrency)
        self._snapshot = RpcSnapshot()
        self._task: asyncio.Task[None] | None = None

    @property
    def configs(self) -> tuple[RpcConfig, ...]:
        return self._configs

    @property
    def registry(self) -> ChainRegistry:
        return self._registry

    @property
    def snapshot(self) -> RpcSnapshot:
        return self._snapshot

    async def _check_with_limit(
        self, client: BaseHttpClient, url: SecureUrl | str, chain: Chain
    ) -> RpcStatus:
        async with self._semaphore:
            return await check_rpc(client, url, chain)

    async def poll_once(self) -> None:
        async with HttpClient() as client:
            tasks = [
                self._check_with_limit(client, url, chain)
                for rpc_config in self._configs
                if (chain := self._registry.lookup(rpc_config.chain)) is not None
                for url in rpc_config.urls
            ]

            results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, RpcStatus):
                self._snapshot.endpoints[result.url] = result

        self._snapshot.updated_at = time.time()

    async def _run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:
                logger.exception("rpc poll failed")
            await asyncio.sleep(self._poll_interval)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
