"""Proxy pool rotation over the KAP WAF's per-IP budget.

The WAF blocks per source IP (RemoteProtocolError), and the budget refills with wall time.
A rotating pool exploits that: a spent IP is parked to refill while another carries on.
These tests pin the two things that must hold - the no-proxy path is byte-for-byte the old
behaviour, and a block rotates to a fresh IP rather than failing the request - without
touching the network.
"""
import time

import httpx
import pytest

from trailing_edge.core import http as http_mod
from trailing_edge.core.http import RateLimitedClient, _load_proxies


@pytest.fixture(autouse=True)
def _no_ambient_proxies(monkeypatch, tmp_path):
    """Keep a developer's real proxies.txt or KAP_PROXIES out of the unit tests."""
    monkeypatch.delenv("KAP_PROXIES", raising=False)
    monkeypatch.chdir(tmp_path)


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
async def test_a_blocked_ip_rotates_to_a_fresh_one(monkeypatch):
    """The whole point: IP 0 throws the WAF disconnect, and the request succeeds through
    IP 1 rather than failing. IP 0 is parked cooling."""
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
async def test_all_ips_cooling_surfaces_the_waf_condition(monkeypatch):
    """When every IP is spent, the pool has nothing to give: surface RemoteProtocolError so
    the scraper defers, rather than busy-looping."""
    monkeypatch.setenv("KAP_PROXIES", "http://ip0:1,http://ip1:1")
    client = RateLimitedClient()

    async with client:
        async def boom(*a, **k):
            raise httpx.RemoteProtocolError("Server disconnected")

        for c in client._clients:
            monkeypatch.setattr(c, "request", boom)

        with pytest.raises(httpx.RemoteProtocolError):
            await client.get("https://kap.org.tr/x")

        # both parked
        assert all(t > time.monotonic() for t in client._cooling_until)

        # a second call, with every IP still cooling, also surfaces rather than hanging
        with pytest.raises(httpx.RemoteProtocolError):
            await client.get("https://kap.org.tr/x")


@pytest.mark.asyncio
async def test_a_healthy_ip_is_drained_not_round_robined(monkeypatch):
    """Requests are sequential, so the pool does not parallelise - its value is avoiding
    the block stall. The right policy is therefore to use one IP at full speed until it
    blocks and only then rotate, so each IP's whole ~50-request budget is spent before we
    move on. An IP that keeps succeeding keeps serving."""
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

        for _ in range(3):
            await client.get("https://kap.org.tr/x")

        assert seen == [0, 0, 0]  # drained, not spread


@pytest.mark.asyncio
async def test_rotation_advances_only_when_the_current_ip_blocks(monkeypatch):
    """IP0 serves once, then blocks; the retry lands on IP1, which then serves the rest.
    So the sequence is 0 (ok), 0 (block -> rotate), 1 (ok), 1 (ok)."""
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

        assert seen == [0, 0, 1, 1]
