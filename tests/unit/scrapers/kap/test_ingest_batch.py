"""Concurrent ingest of a batch of disclosures.

The batch helper is what turns the proxy pool into real parallelism: up to _CONCURRENCY
disclosures are in flight at once. These pin the two things that must not break when the
sequential loop became a concurrent gather - every failure is still collected for retry, and
the concurrency bound is actually respected.
"""
import asyncio

import pytest

from trailing_edge.scrapers.kap import insider as insider_mod
from trailing_edge.scrapers.kap.insider import KapInsiderScraper, _Counts


@pytest.mark.asyncio
async def test_failures_are_collected_and_successes_are_not(monkeypatch):
    scraper = KapInsiderScraper(backfill=True)
    discs = [{"disclosureIndex": i} for i in range(10)]

    async def fake_ingest_one(kap, disc, counts, already):
        # odd indices fail (transient), even ones succeed
        if disc["disclosureIndex"] % 2 == 1:
            raise RuntimeError("Server disconnected")

    monkeypatch.setattr(scraper, "_ingest_one", fake_ingest_one)

    deferred = await scraper._ingest_batch(
        kap=None, discs=discs, counts=_Counts(), already_stored=set(),
        final=False, attempt=0,
    )

    got = sorted(d["disclosureIndex"] for d in deferred)
    assert got == [1, 3, 5, 7, 9], "every failed disclosure must be returned for retry"


@pytest.mark.asyncio
async def test_all_succeed_leaves_nothing_deferred(monkeypatch):
    scraper = KapInsiderScraper(backfill=True)
    discs = [{"disclosureIndex": i} for i in range(5)]

    async def ok(kap, disc, counts, already):
        return None

    monkeypatch.setattr(scraper, "_ingest_one", ok)

    deferred = await scraper._ingest_batch(
        kap=None, discs=discs, counts=_Counts(), already_stored=set(),
        final=False, attempt=0,
    )
    assert deferred == []


@pytest.mark.asyncio
async def test_concurrency_bound_is_respected(monkeypatch):
    """No more than _CONCURRENCY disclosures may be in flight at once - the bound protects
    the DB connection pool and keeps the fan-out onto a public server civil."""
    monkeypatch.setattr(insider_mod, "_CONCURRENCY", 3)
    scraper = KapInsiderScraper(backfill=True)
    discs = [{"disclosureIndex": i} for i in range(12)]

    in_flight = 0
    peak = 0

    async def slow(kap, disc, counts, already):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1

    monkeypatch.setattr(scraper, "_ingest_one", slow)

    await scraper._ingest_batch(
        kap=None, discs=discs, counts=_Counts(), already_stored=set(),
        final=False, attempt=0,
    )
    assert peak <= 3, f"concurrency bound exceeded: {peak} in flight"
    assert peak == 3, "and it should actually reach the bound with 12 items and sleep"
