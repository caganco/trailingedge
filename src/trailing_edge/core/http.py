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

# Proactive pacing, to stay UNDER the WAF budget rather than crashing into it. Measured over
# 87 windows in one backfill, requests between one block and the next ran to a median of 161
# (p10 = 43 - the budget is noisy, not a clean token bucket). So a spent IP does not have to
# be the trigger: after this many requests we pause for the refill window on our own, and the
# block - with its deferrals, its PARTIAL month, and the recovery sweep that PARTIAL forces -
# mostly never happens. Pacing under the budget is what lets a month finish SUCCESS on the
# first pass. Set KAP_PACE_EVERY=0 to disable.
_PACE_EVERY = int(os.environ.get("KAP_PACE_EVERY", "120"))
_PACE_SLEEP_S = float(os.environ.get("KAP_PACE_SLEEP_S", "120"))

# Requests made on each IP since its last pause, keyed by proxy URL (None = direct). This is
# MODULE-level on purpose: the backfill builds a fresh RateLimitedClient per month, but the
# WAF budget is per-IP and global across months. A per-client counter would reset every month
# and never fire on the sparse recent months, which is exactly where it was needed - a run of
# short months would spend the shared budget between them and block. Keyed by IP so it still
# composes with a proxy pool.
_PACE_STATE: dict[str | None, int] = {}


def _reset_pace_state() -> None:
    """Clear the module-level pace counters. For tests; not used in normal operation."""
    _PACE_STATE.clear()


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

    def _acquire(self) -> int | None:
        """Index of an IP that is ready to serve: neither cooling after a block nor at its
        pace budget. An IP that has reached _PACE_EVERY requests is PARKED here - set cooling
        for the refill window and skipped - rather than slept on. That is what makes the pool
        fast: with one IP, parking it leaves nothing else and the caller waits (the proactive
        pause); with ten, the other nine keep serving while it refills, so we rarely wait at
        all. None when every IP is currently cooling or parked."""
        n = len(self._clients)
        now = time.monotonic()
        for step in range(n):
            i = (self._idx + step) % n
            if self._cooling_until[i] > now:
                continue
            ip = self._proxies[i]
            if _PACE_EVERY > 0 and _PACE_STATE.get(ip, 0) >= _PACE_EVERY:
                self._cooling_until[i] = now + _PACE_SLEEP_S
                _PACE_STATE[ip] = 0  # the refill wait restores the budget
                _log.info("pace_pause", proxy_index=i, after_requests=_PACE_EVERY, sleep_s=_PACE_SLEEP_S)
                continue
            # Advance the cursor so the NEXT acquire starts at the following IP. Sequentially
            # this spreads load round-robin (each IP's budget lasts n times longer in wall
            # time); under concurrency it hands each in-flight request a different IP, which
            # is what turns the pool from a failover into real parallelism.
            self._idx = (i + 1) % n
            return i
        return None

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self._request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self._request("POST", url, **kwargs)

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        assert self._clients, "Use as async context manager"
        single = len(self._clients) == 1
        blocks = 0

        while True:
            i = self._acquire()
            if i is None:
                # Every IP is cooling or parked. Wait until the soonest is free, then retry.
                # For a single IP this realises the proactive pace pause; for a pool it means
                # the whole pool is momentarily spent.
                wait = min(self._cooling_until) - time.monotonic()
                await asyncio.sleep(max(wait, 0.05))
                continue
            try:
                resp = await self._request_via(i, method, url, **kwargs)
                ip = self._proxies[i]
                _PACE_STATE[ip] = _PACE_STATE.get(ip, 0) + 1
                return resp
            except httpx.RemoteProtocolError:
                # This IP's budget is spent (or it is genuinely the WAF). Park it to refill.
                self._cooling_until[i] = time.monotonic() + _IP_COOLDOWN_S
                _PACE_STATE[self._proxies[i]] = 0
                self._idx = (i + 1) % len(self._clients)
                if single:
                    # Unchanged single-IP contract: surface immediately so the scraper defers.
                    raise
                _log.info("proxy_ip_cooling", proxy_index=i, cooldown_s=_IP_COOLDOWN_S)
                blocks += 1
                if blocks >= len(self._clients):
                    # The whole pool blocked within this one call - nothing left to try now.
                    # Surface it so the scraper defers rather than busy-looping.
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
