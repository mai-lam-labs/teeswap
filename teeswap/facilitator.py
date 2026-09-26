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
from collections.abc import Mapping
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


class FacilitatorsDownError(TeeSwapError):
    """Every facilitator that can settle a payment is down right now: try again soon."""


# after a failed poll, how soon to look again: sooner than the poll interval, so one failed
# poll doesn't take a facilitator out for long
FAILED_POLL_RETRY_SECONDS = 10.0


# how fast a facilitator's record fades: what happened an hour ago counts half as much
RELIABILITY_HALF_LIFE_SECONDS = 3600.0
# what's assumed before there's a record: one success, so a new or rarely used facilitator
# ranks well rather than being penalised for having been quiet
PRIOR_SUCCESSES = 1.0
PRIOR_FAILURES = 0.0


@dataclass(slots=True)
class Reliability:
    """How a facilitator has been doing lately: counts of what it did and didn't do that
    fade with a half-life, so an old failure stops counting. Constant-sized; facilitators
    are ranked by their success rate, never dropped for their failures."""

    successes: float = 0.0
    failures: float = 0.0
    updated_at: float = field(default_factory=time.monotonic)

    def record(self, ok: bool) -> None:
        weight = self._weight(time.monotonic())
        self.successes *= weight
        self.failures *= weight
        self.updated_at = time.monotonic()
        if ok:
            self.successes += 1
        else:
            self.failures += 1

    @property
    def success_rate(self) -> float:
        weight = self._weight(time.monotonic())
        successes = self.successes * weight + PRIOR_SUCCESSES
        return successes / (successes + self.failures * weight + PRIOR_FAILURES)

    def _weight(self, now: float) -> float:
        return 0.5 ** ((now - self.updated_at) / RELIABILITY_HALF_LIFE_SECONDS)


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
    known: FacilitatorStatus | None = None,
) -> FacilitatorStatus:
    """Whether the facilitator is up, and what it can settle. One that's down keeps what it
    was last known to settle (`known`): it's down, not incapable."""
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
            kinds=() if known is None else known.kinds,
            extensions=() if known is None else known.extensions,
            signers=() if known is None else known.signers,
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
                known = self._snapshot.facilitators.get(self._url)
                status = await poll_facilitator(client, self._url, known)
                self._snapshot.facilitators[self._url] = status
                self._snapshot.updated_at = time.time()
                interval = self._config.poll_interval
                await asyncio.sleep(interval if status.healthy else FAILED_POLL_RETRY_SECONDS)

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
        # how each has done for every job, as they reported it
        self._reliability = {c.url: Reliability() for c in configs}
        self._pollers = [
            _FacilitatorPoller(c, self._snapshot, initial_delay=i * STAGGER_INTERVAL)
            for i, c in enumerate(configs)
        ]

    @property
    def snapshot(self) -> FacilitatorSnapshot:
        return self._snapshot

    def record(self, url: str, ok: bool) -> None:
        """How a facilitator did with a request: it counts toward its ranking for everyone."""
        self._reliability.setdefault(url, Reliability()).record(ok)

    def candidates(
        self, scheme: str, network: str, job: Mapping[str, Reliability]
    ) -> tuple[str, ...]:
        """Healthy facilitators that support (scheme, network), best first: by how they
        did for this job, then for every job, then in config order. A failure only moves a
        facilitator down; it's still tried when the others fail too."""
        capable = {f.url for f in self._snapshot.facilitators_for(scheme, network)}
        if not capable and any(
            k.scheme == scheme and k.network == network
            for f in self._snapshot.facilitators.values()
            for k in f.kinds
        ):
            raise FacilitatorsDownError(f"every facilitator for {scheme} on {network} is down")
        ranked = [
            (-_success_rate(job, c.url), -_success_rate(self._reliability, c.url), i, c.url)
            for i, c in enumerate(self._configs)
            if c.url in capable
        ]
        return tuple(url for *_, url in sorted(ranked))

    def start(self) -> None:
        for p in self._pollers:
            p.start()

    def stop(self) -> None:
        for p in self._pollers:
            p.stop()


def _success_rate(record: Mapping[str, Reliability], url: str) -> float:
    reliability = record.get(url)
    return Reliability().success_rate if reliability is None else reliability.success_rate


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
