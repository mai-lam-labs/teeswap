"""x402 facilitator monitor — polls configured facilitators for liveness and capabilities.

The monitor maintains a live snapshot of which currencies, chains, and payment
schemes are available across all configured facilitators. The rest of the system
reads this snapshot when constructing 402 payment requirements.

Liveness and capabilities are determined from a single GET /supported call.
If it responds, the facilitator is alive and we have its capabilities.

See: https://docs.x402.org/core-concepts/facilitator
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .common import TeeSwapError
from .config import FacilitatorConfig
from .http import BaseHttpClient
from .wire import WireError, decode_object, encode, parse_json
from .x402 import X402_VERSION, PaymentPayload, PaymentRequirements, SettleResponse, VerifyResponse

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

REQUEST_TIMEOUT = 10.0
SETTLE_TIMEOUT = 60.0


class FacilitatorError(TeeSwapError):
    """The facilitator couldn't be reached or answered with something we can't read."""


@dataclass(frozen=True, slots=True)
class PaymentKind:
    scheme: str
    network: str


@dataclass(frozen=True, slots=True)
class FacilitatorStatus:
    url: str
    healthy: bool
    kinds: tuple[PaymentKind, ...]
    extensions: tuple[str, ...]
    signers: tuple[str, ...]
    last_polled: float


@dataclass(slots=True)
class FacilitatorSnapshot:
    facilitators: dict[str, FacilitatorStatus] = field(default_factory=dict)
    updated_at: float = 0.0

    @property
    def available_kinds(self) -> list[PaymentKind]:
        seen: set[tuple[str, str]] = set()
        result: list[PaymentKind] = []
        for f in self.facilitators.values():
            if not f.healthy:
                continue
            for kind in f.kinds:
                key = (kind.scheme, kind.network)
                if key not in seen:
                    seen.add(key)
                    result.append(kind)
        return result

    @property
    def available_networks(self) -> set[str]:
        return {k.network for k in self.available_kinds}

    @property
    def available_schemes(self) -> set[str]:
        return {k.scheme for k in self.available_kinds}

    def facilitators_for(self, scheme: str, network: str) -> list[FacilitatorStatus]:
        return [
            f
            for f in self.facilitators.values()
            if f.healthy and any(k.scheme == scheme and k.network == network for k in f.kinds)
        ]


async def poll_facilitator(
    client: httpx.AsyncClient,
    url: str,
) -> FacilitatorStatus:
    now = time.time()
    try:
        resp = await client.get(f"{url}/supported", timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
    except (httpx.HTTPError, httpx.TimeoutException, ValueError) as e:
        logger.warning("facilitator %s: %s", url, e)
        return FacilitatorStatus(
            url=url,
            healthy=False,
            kinds=(),
            extensions=(),
            signers=(),
            last_polled=now,
        )

    kinds = tuple(
        PaymentKind(scheme=k.get("scheme", ""), network=k.get("network", ""))
        for k in data.get("kinds", [])
    )
    extensions = tuple(data.get("extensions", []))
    signers = tuple(data.get("signers", []))

    logger.info("facilitator %s: %d kinds, %d extensions", url, len(kinds), len(extensions))
    return FacilitatorStatus(
        url=url,
        healthy=True,
        kinds=kinds,
        extensions=extensions,
        signers=signers,
        last_polled=now,
    )


class _FacilitatorPoller:
    def __init__(
        self, config: FacilitatorConfig, snapshot: FacilitatorSnapshot, initial_delay: float = 0.0
    ) -> None:
        self._config = config
        self._snapshot = snapshot
        self._url = config.url
        self._initial_delay = initial_delay
        self._task: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        if self._initial_delay > 0:
            await asyncio.sleep(self._initial_delay)
        async with httpx.AsyncClient() as client:
            while True:
                status = await poll_facilitator(client, self._url)
                self._snapshot.facilitators[self._url] = status
                self._snapshot.updated_at = time.time()
                await asyncio.sleep(self._config.poll_interval)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()


STAGGER_INTERVAL = 2.0


class FacilitatorMonitor:
    def __init__(self, configs: tuple[FacilitatorConfig, ...]) -> None:
        self._configs = configs
        self._snapshot = FacilitatorSnapshot()
        self._pollers = [
            _FacilitatorPoller(c, self._snapshot, initial_delay=i * STAGGER_INTERVAL)
            for i, c in enumerate(configs)
        ]

    @property
    def snapshot(self) -> FacilitatorSnapshot:
        return self._snapshot

    def candidates(self, scheme: str, network: str, excluded: set[str]) -> tuple[str, ...]:
        """Healthy facilitators that support (scheme, network), in config order."""
        capable = {f.url for f in self._snapshot.facilitators_for(scheme, network)}
        return tuple(c.url for c in self._configs if c.url in capable and c.url not in excluded)

    def start(self) -> None:
        for p in self._pollers:
            p.start()

    def stop(self) -> None:
        for p in self._pollers:
            p.stop()


# --- Verify and settle (x402 facilitator API) ---


async def verify(
    client: BaseHttpClient, url: str, payload: PaymentPayload, requirements: PaymentRequirements
) -> VerifyResponse:
    body = await _post(client, f"{url}/verify", payload, requirements, REQUEST_TIMEOUT)
    return VerifyResponse.from_dict(body)


async def settle(
    client: BaseHttpClient, url: str, payload: PaymentPayload, requirements: PaymentRequirements
) -> SettleResponse:
    body = await _post(client, f"{url}/settle", payload, requirements, SETTLE_TIMEOUT)
    return SettleResponse.from_dict(body)


async def _post(
    client: BaseHttpClient,
    url: str,
    payload: PaymentPayload,
    requirements: PaymentRequirements,
    timeout: float,
) -> dict[str, Any]:
    request = {
        "x402Version": X402_VERSION,
        "paymentPayload": payload,
        "paymentRequirements": requirements,
    }
    try:
        # our encoder decides the wire form (amounts as strings); httpx only carries it
        resp = await client.post(url, json=parse_json(encode(request)), timeout=timeout)
        return decode_object(resp.content)
    except (httpx.HTTPError, WireError) as e:
        raise FacilitatorError(f"{url}: {e}") from e
