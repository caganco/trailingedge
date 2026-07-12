"""Rate-limited, retry-capable httpx async client wrapper."""
import asyncio
from types import TracebackType
from typing import Any

import httpx
from aiolimiter import AsyncLimiter
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from trailing_edge.core.config import get_config
from trailing_edge.core.logging import get_logger

_log = get_logger(__name__)


def _is_retryable(exc: BaseException) -> bool:
    """Which failures are worth retrying *inline*, right where they happened.

    httpx.RemoteProtocolError ("Server disconnected without sending a response") is
    deliberately NOT in this set. On KAP that error is not a network blip - it is the
    WAF cutting the connection, and it does not lift in seconds. Retrying it inline
    with exponential backoff (the old policy: 5 attempts, 4+8+16+32s) spent up to a
    minute per occurrence re-poking the block, still failed ~12% of the time, and the
    caller then dropped the disclosure. Measured over one backfill: 575 such failures,
    304 minutes burned - 83% of the wall clock - and 12% of the data silently lost.

    A WAF disconnect is instead surfaced immediately so the scraper can defer that
    disclosure, wait out the throttle once per chunk, and retry it then (see
    KapInsiderScraper: _WAF_COOLDOWN_S). Fail fast here, recover properly there.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 503)
    return isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout))


class RateLimitedClient:
    def __init__(self) -> None:
        cfg = get_config()
        kap = cfg["kap"]
        self._limiter = AsyncLimiter(float(kap["rate_limit_rps"]), 1.0)
        self._timeout = float(kap["timeout_s"])
        self._headers = {
            "User-Agent": kap["user_agent"],
            "Accept-Language": "tr",
        }
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "RateLimitedClient":
        self._client = httpx.AsyncClient(
            headers=self._headers,
            timeout=self._timeout,
            follow_redirects=True,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._client:
            await self._client.aclose()

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self._request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self._request("POST", url, **kwargs)

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        assert self._client is not None, "Use as async context manager"
        client = self._client

        @retry(
            retry=retry_if_exception(_is_retryable),
            stop=stop_after_attempt(5),
            wait=wait_exponential(multiplier=2, min=4, max=60),
            reraise=True,
        )
        async def _do() -> httpx.Response:
            async with self._limiter:
                resp = await client.request(method, url, **kwargs)
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", "5"))
                    _log.warning("rate_limited", url=url, retry_after=retry_after)
                    await asyncio.sleep(retry_after)
                    resp.raise_for_status()
                resp.raise_for_status()
                return resp

        return await _do()
