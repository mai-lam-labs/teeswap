"""HTTP client with secret redaction and optional recording.

HttpClient wraps httpx.AsyncClient, handles URL template expansion and
secret headers. RecordingClient extends HttpClient and logs every exchange
with secrets redacted.
"""

import abc
import json as _json
from datetime import UTC, datetime
from typing import Any, Self, override

import httpx

from .types import HttpExchange, Millis, Timestamp, Url


class BaseHttpClient(abc.ABC):
    @abc.abstractmethod
    async def post(
        self,
        url: Url,
        *,
        json: Any = None,
        timeout: float = 10.0,
    ) -> httpx.Response: ...

    @abc.abstractmethod
    async def get(
        self,
        url: Url,
        *,
        timeout: float = 10.0,
    ) -> httpx.Response: ...


def _resolve(url: Url) -> tuple[str, str, dict[str, str]]:
    if isinstance(url, str):
        return url, url, {}
    expanded = url.url
    for key, value in url.url_secrets.items():
        expanded = expanded.replace(f"{{{key}}}", value)
    return expanded, url.url, dict(url.secret_headers)


class HttpClient(BaseHttpClient):
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_client:
            await self._client.aclose()

    @override
    async def post(
        self,
        url: Url,
        *,
        json: Any = None,
        timeout: float = 10.0,
    ) -> httpx.Response:
        real_url, _, headers = _resolve(url)
        return await self._client.post(real_url, json=json, timeout=timeout, headers=headers)

    @override
    async def get(
        self,
        url: Url,
        *,
        timeout: float = 10.0,
    ) -> httpx.Response:
        real_url, _, headers = _resolve(url)
        return await self._client.get(real_url, timeout=timeout, headers=headers)


class RecordingClient(HttpClient):
    def __init__(
        self, exchanges: list[HttpExchange], client: httpx.AsyncClient | None = None
    ) -> None:
        super().__init__(client)
        self._exchanges = exchanges

    @override
    async def post(
        self,
        url: Url,
        *,
        json: Any = None,
        timeout: float = 10.0,
    ) -> httpx.Response:
        start = datetime.now(UTC)
        resp = await super().post(url, json=json, timeout=timeout)
        _, template, _ = _resolve(url)
        self._record(start, "POST", template, json, resp)
        return resp

    @override
    async def get(
        self,
        url: Url,
        *,
        timeout: float = 10.0,
    ) -> httpx.Response:
        start = datetime.now(UTC)
        resp = await super().get(url, timeout=timeout)
        _, template, _ = _resolve(url)
        self._record(start, "GET", template, None, resp)
        return resp

    def _record(
        self,
        start: datetime,
        method: str,
        template_url: str,
        request_body: Any,
        resp: httpx.Response,
    ) -> None:
        elapsed = (datetime.now(UTC) - start).total_seconds() * 1000
        self._exchanges.append(
            HttpExchange(
                timestamp=Timestamp(start),
                method=method,
                url=template_url,
                request_headers={},
                request_body=_json.dumps(request_body).encode()
                if request_body is not None
                else None,
                status_code=resp.status_code,
                response_headers=dict(resp.headers),
                response_body=resp.content,
                latency=Millis(int(elapsed)),
            )
        )
