"""Proxy pool rotation over the KAP WAF's per-IP budget.

The WAF blocks per source IP (RemoteProtocolError), and the budget refills with wall time.
A rotating pool exploits that: an IP that has spent its budget - whether it actually blocked,
or reached the proactive pace limit - is PARKED to refill while the other IPs keep serving.
With one IP, parking it leaves nothing else and the caller waits (the proactive pause). With
ten, we almost never wait. These tests pin: the no-proxy path still surfaces a WAF disconnect
straight to the scraper; a pace limit parks-and-rotates; a block parks-and-rotates; and a
whole pool blocking within one call still surfaces so the scraper defers.
"""
import time

import httpx
import pytest

from trailing_edge.core import http as http_mod
from trailing_edge.core.http import RateLimitedClient, _load_proxies


@pytest.fixture(autouse=True)
def _no_ambient_proxies(monkeypatch, tmp_path):
    """Keep a developer's real proxies.txt or KAP_PROXIES out of the unit tests, hold the
    proactive pacer off by default so rotation tests do not sleep, and clear the module-level
    pace counters so state cannot leak between tests."""
    monkeypatch.delenv("KAP_PROXIES", raising=False)
    monkeypatch.setattr(http_mod, "_PACE_EVERY", 0)
    http_mod._reset_pace_state()
    monkeypatch.chdir(tmp_path)
    yield
    http_mod._reset_pace_state()


def test_no_proxy_configured_means_one_direct_connection():
    assert _load_proxies() == [None]


def test_env_var_is_read_as_a_comma_separated_pool(monkeypatch):
    monkeypatch.setenv("KAP_PROXIES", "http://a:1, http://b:2 ,http://c:3")
    assert _load_proxies() == ["http://a:1", "http://b:2", "http://c:3"]


def test_proxies_file_is_read_when_env_is_absent(tmp_path, monkeypatch):
    (tmp_path / "proxies.txt").write_text(
        "# a comment\nhttp://a:1\n\nhttp://b:2\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    assert _load_proxies() == ["http://a:1", "http://b:2"]


@pytest.mark.asyncio
async def test_single_client_surfaces_waf_disconnect_unchanged(monkeypatch):
    """With no pool, a RemoteProtocolError must reach the caller as-is - the scraper's
    defer-and-cooldown path depends on it, and adding the pool must not change it."""
    client = RateLimitedClient()
    assert client._proxies == [None]

    async with client:
        calls = {"n": 0}

        async def boom(*a, **k):
            calls["n"] += 1
            raise httpx.RemoteProtocolError("Server disconnected")

        monkeypatch.setattr(client._clients[0], "request", boom)

        with pytest.raises(httpx.RemoteProtocolError):
            await client.get("https://kap.org.tr/x")
        # surfaced immediately, not retried inline
        assert calls["n"] == 1


@pytest.mark.asyncio
async def test_the_pacer_pauses_before_the_budget_is_spent(monkeypatch):
    """After _PACE_EVERY requests on the one IP, the next request finds it parked and waits
    out the refill window - one proactive pause. This is what keeps a month finishing SUCCESS
    on the first pass rather than going PARTIAL and needing a sweep."""
    monkeypatch.setattr(http_mod, "_PACE_EVERY", 3)
    monkeypatch.setattr(http_mod, "_PACE_SLEEP_S", 0.0)

    client = RateLimitedClient()
    async with client:
        slept: list[float] = []

        async def fake_sleep(s):
            slept.append(s)

        monkeypatch.setattr(http_mod.asyncio, "sleep", fake_sleep)

        async def ok(*a, **k):
            return httpx.Response(200, request=httpx.Request("GET", "https://k/x"))

        monkeypatch.setattr(client._clients[0], "request", ok)

        # 3 requests fit under the budget, the 4th finds the IP parked and waits once
        for _ in range(4):
            await client.get("https://kap.org.tr/x")

    assert len(slept) == 1, "exactly one proactive pause after PACE_EVERY requests"


@pytest.mark.asyncio
async def test_pace_state_survives_a_fresh_client_across_chunks(monkeypatch):
    """The bug this pins: the backfill builds a new RateLimitedClient per month, but the WAF
    budget is global per-IP across months. When the counter lived on the client it reset
    every month and never fired on the sparse recent months - a run of short months spent
    the shared budget between them and blocked. The counter is module-level and keyed by IP,
    so two requests through one client and two through the next together trip the pace at the
    4th, not restart the count."""
    monkeypatch.setattr(http_mod, "_PACE_EVERY", 3)
    monkeypatch.setattr(http_mod, "_PACE_SLEEP_S", 0.0)

    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(http_mod.asyncio, "sleep", fake_sleep)

    async def ok(*a, **k):
        return httpx.Response(200, request=httpx.Request("GET", "https://k/x"))

    async with RateLimitedClient() as c1:
        monkeypatch.setattr(c1._clients[0], "request", ok)
        await c1.get("https://kap.org.tr/x")
        await c1.get("https://kap.org.tr/x")

    async with RateLimitedClient() as c2:
        monkeypatch.setattr(c2._clients[0], "request", ok)
        await c2.get("https://kap.org.tr/x")
        await c2.get("https://kap.org.tr/x")

    # 4 requests on the one IP crossed the pace-of-3 once, despite the client being rebuilt
    assert len(slept) == 1, "pace must accumulate across client instances, not reset"


@pytest.mark.asyncio
async def test_a_pace_limited_ip_rotates_instead_of_sleeping(monkeypatch):
    """The change that makes the pool fast. With more than one IP, hitting the pace limit
    must NOT block the caller - the spent IP is parked and the next fresh IP serves the
    request immediately, no sleep."""
    monkeypatch.setenv("KAP_PROXIES", "http://ip0:1,http://ip1:1,http://ip2:1")
    monkeypatch.setattr(http_mod, "_PACE_EVERY", 2)
    monkeypatch.setattr(http_mod, "_PACE_SLEEP_S", 999.0)  # long, to prove we do NOT sleep it

    client = RateLimitedClient()
    async with client:
        slept: list[float] = []

        async def fake_sleep(s):
            slept.append(s)

        monkeypatch.setattr(http_mod.asyncio, "sleep", fake_sleep)

        seen: list[int] = []

        def make(idx):
            async def h(*a, **k):
                seen.append(idx)
                return httpx.Response(200, request=httpx.Request("GET", "https://k/x"))
            return h

        for i, c in enumerate(client._clients):
            monkeypatch.setattr(c, "request", make(i))

        # Round-robin spreads the 6 requests two full cycles across the 3 IPs. Each IP reaches
        # its pace budget on the second cycle but is only parked on the NEXT acquire, so no
        # request in this batch has to sleep - a fresh IP is always available.
        for _ in range(6):
            await client.get("https://kap.org.tr/x")

        assert seen == [0, 1, 2, 0, 1, 2]
        assert slept == [], "a pool must rotate on pace, never sleep while a fresh IP exists"


@pytest.mark.asyncio
async def test_a_blocked_ip_rotates_to_a_fresh_one(monkeypatch):
    """IP 0 throws the WAF disconnect, and the request succeeds through IP 1 rather than
    failing. IP 0 is parked cooling."""
    monkeypatch.setenv("KAP_PROXIES", "http://ip0:1,http://ip1:1,http://ip2:1")
    client = RateLimitedClient()

    async with client:
        ok = httpx.Response(200, request=httpx.Request("GET", "https://kap.org.tr/x"))

        async def ip0(*a, **k):
            raise httpx.RemoteProtocolError("Server disconnected")

        async def ip1(*a, **k):
            return ok

        monkeypatch.setattr(client._clients[0], "request", ip0)
        monkeypatch.setattr(client._clients[1], "request", ip1)

        resp = await client.get("https://kap.org.tr/x")
        assert resp.status_code == 200
        assert client._cooling_until[0] > time.monotonic()  # IP0 parked
        assert client._cooling_until[1] == 0.0  # IP1 clean


@pytest.mark.asyncio
async def test_whole_pool_blocking_in_one_call_surfaces_the_waf(monkeypatch):
    """When every IP in the pool blocks within a single call, there is nothing left to try
    now: surface RemoteProtocolError so the scraper defers, rather than busy-looping."""
    monkeypatch.setenv("KAP_PROXIES", "http://ip0:1,http://ip1:1")
    client = RateLimitedClient()

    async with client:
        async def boom(*a, **k):
            raise httpx.RemoteProtocolError("Server disconnected")

        for c in client._clients:
            monkeypatch.setattr(c, "request", boom)

        with pytest.raises(httpx.RemoteProtocolError):
            await client.get("https://kap.org.tr/x")

        assert all(t > time.monotonic() for t in client._cooling_until)  # both parked


@pytest.mark.asyncio
async def test_healthy_ips_are_used_round_robin(monkeypatch):
    """Each acquire advances the cursor, so successive requests spread across the pool. This
    is what lets concurrency hand each in-flight request a different IP, and it also makes a
    single IP's budget last n times longer in wall time."""
    monkeypatch.setenv("KAP_PROXIES", "http://ip0:1,http://ip1:1,http://ip2:1")
    client = RateLimitedClient()

    async with client:
        seen: list[int] = []

        def make(idx):
            async def h(*a, **k):
                seen.append(idx)
                return httpx.Response(200, request=httpx.Request("GET", "https://k/x"))
            return h

        for i, c in enumerate(client._clients):
            monkeypatch.setattr(c, "request", make(i))

        for _ in range(6):
            await client.get("https://kap.org.tr/x")

        assert seen == [0, 1, 2, 0, 1, 2]  # round-robin across the pool


@pytest.mark.asyncio
async def test_a_block_parks_and_the_call_still_succeeds_under_round_robin(monkeypatch):
    """Round-robin picks IP0, IP1, then IP0 again - and on that third request IP0 blocks, so
    the call parks it and completes on IP1. Sequence: 0 (ok), 1 (ok), 0 (block -> rotate),
    1 (ok)."""
    monkeypatch.setenv("KAP_PROXIES", "http://ip0:1,http://ip1:1")
    client = RateLimitedClient()

    async with client:
        seen: list[int] = []
        ip0_calls = {"n": 0}

        async def ip0(*a, **k):
            ip0_calls["n"] += 1
            seen.append(0)
            if ip0_calls["n"] >= 2:
                raise httpx.RemoteProtocolError("Server disconnected")
            return httpx.Response(200, request=httpx.Request("GET", "https://k/x"))

        async def ip1(*a, **k):
            seen.append(1)
            return httpx.Response(200, request=httpx.Request("GET", "https://k/x"))

        monkeypatch.setattr(client._clients[0], "request", ip0)
        monkeypatch.setattr(client._clients[1], "request", ip1)

        for _ in range(3):
            await client.get("https://kap.org.tr/x")

        assert seen == [0, 1, 0, 1]
        assert client._cooling_until[0] > time.monotonic()  # IP0 parked after its block
