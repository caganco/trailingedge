"""Rate-limited, retry-capable httpx async client wrapper.

Optionally rotates over a pool of proxies. The KAP WAF throttles per source IP: a block
arrives as httpx.RemoteProtocolError on the connection, and the budget (~50 requests)
refills with wall-clock time, not with a slower request rate - measured over 66 blocks in
one backfill, a 20-minute pause bought the same ~50 requests as a 90-second one. That is
exactly the limit a rotating IP pool defeats: when one IP's budget is spent, set it aside
to refill and carry on through another. With no proxy configured the client behaves exactly
as before - one direct connection, RemoteProtocolError surfaced immediately for the scraper
to defer.
"""
import asyncio
import os
import time
from pathlib import Path
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

# How long a proxy's IP budget takes to refill after a WAF block. Measured: the budget was
# back within ~2 minutes regardless of how much longer we waited. A blocked IP is parked for
# this long before the rotation returns to it.
_IP_COOLDOWN_S = 120.0


def _load_proxies() -> list[str | None]:
    """Proxy URLs for the rotation pool, or ``[None]`` for a direct connection.

    Sources, in order: the ``KAP_PROXIES`` env var (comma-separated), else a ``proxies.txt``
    file in the working directory (one URL per line, ``#`` comments allowed). The file is
    gitignored and must never be committed - a proxy list is a credential.

    Returning ``[None]`` means "no pool": one direct connection, behaviour unchanged.
    """
    raw = os.environ.get("KAP_PROXIES", "").strip()
    entries: list[str] = []
    if raw:
        entries = [p.strip() for p in raw.split(",") if p.strip()]
    else:
        f = Path("proxies.txt")
        if f.is_file():
            entries = [
                ln.strip()
                for ln in f.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.strip().startswith("#")
            ]
    return list(entries) if entries else [None]


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

    When a proxy pool is configured this same error first triggers an IP rotation (see
    _request); it only reaches the scraper once every IP in the pool is cooling.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 503)
    return isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout))


class RateLimitedClient:
    def __init__(self) -> None:
        cfg = get_config()
        kap = cfg["kap"]
        self._rps = float(kap["rate_limit_rps"])
        self._timeout = float(kap["timeout_s"])
        self._headers = {
            "User-Agent": kap["user_agent"],
            "Accept-Language": "tr",
        }
        self._proxies = _load_proxies()
        # One client, one limiter, one cooldown clock PER proxy. The WAF budget is per-IP,
        # so the rate limit is too - a global limiter would throttle the whole pool to a
        # single IP's rate and throw away the reason for having a pool.
        self._clients: list[httpx.AsyncClient] = []
        self._limiters: list[AsyncLimiter] = []
        self._cooling_until: list[float] = []
        self._idx = 0

    async def __aenter__(self) -> "RateLimitedClient":
        for proxy in self._proxies:
            self._clients.append(
                httpx.AsyncClient(
                    headers=self._headers,
                    timeout=self._timeout,
                    follow_redirects=True,
                    proxy=proxy,
                )
            )
            self._limiters.append(AsyncLimiter(self._rps, 1.0))
            self._cooling_until.append(0.0)
        if len(self._proxies) > 1:
            _log.info("proxy_pool_active", pool_size=len(self._proxies))
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        for client in self._clients:
            await client.aclose()
        self._clients.clear()

    def _next_available(self) -> int | None:
        """Index of the next proxy whose IP budget is not cooling, round-robin from the
        last one used. None when every IP in the pool is currently cooling."""
        n = len(self._clients)
        now = time.monotonic()
        for step in range(n):
            i = (self._idx + step) % n
            if self._cooling_until[i] <= now:
                self._idx = i
                return i
        return None

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self._request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self._request("POST", url, **kwargs)

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        assert self._clients, "Use as async context manager"

        # Single direct connection (no pool): unchanged from the original - one client, one
        # limiter, RemoteProtocolError surfaced straight to the caller.
        if len(self._clients) == 1:
            return await self._request_via(0, method, url, **kwargs)

        # Pool: a RemoteProtocolError means this IP's budget is spent. Park it to refill and
        # move to a fresh IP, trying each at most once. Only when the whole pool is cooling
        # does the error surface to the scraper, which defers and waits it out as before.
        last_exc: httpx.RemoteProtocolError | None = None
        for _ in range(len(self._clients)):
            i = self._next_available()
            if i is None:
                break
            try:
                return await self._request_via(i, method, url, **kwargs)
            except httpx.RemoteProtocolError as exc:
                last_exc = exc
                self._cooling_until[i] = time.monotonic() + _IP_COOLDOWN_S
                self._idx = (i + 1) % len(self._clients)
                _log.info("proxy_ip_cooling", proxy_index=i, cooldown_s=_IP_COOLDOWN_S)

        if last_exc is not None:
            raise last_exc
        # Every IP was already cooling before we tried any. Surface the WAF condition so the
        # scraper defers, rather than busy-looping on a pool that has nothing to give.
        raise httpx.RemoteProtocolError("all proxy IPs cooling")

    async def _request_via(
        self, i: int, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        client = self._clients[i]
        limiter = self._limiters[i]

        @retry(
            retry=retry_if_exception(_is_retryable),
            stop=stop_after_attempt(5),
            wait=wait_exponential(multiplier=2, min=4, max=60),
            reraise=True,
        )
        async def _do() -> httpx.Response:
            async with limiter:
                resp = await client.request(method, url, **kwargs)
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", "5"))
                    _log.warning("rate_limited", url=url, retry_after=retry_after)
                    await asyncio.sleep(retry_after)
                    resp.raise_for_status()
                resp.raise_for_status()
                return resp

        return await _do()
