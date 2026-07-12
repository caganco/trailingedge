"""KAP's list endpoint truncates at a hard cap and lies about it.

Measured behaviour (2026-07): asking for all of March 2024 returns exactly 2,000 rows
spanning 25-31 March. The first 24 days are silently dropped - no error, no pagination
cursor, no flag. The production backfill chunked by MONTH, so it had been ingesting
roughly the last week of each month and discarding the rest since day one. That, not a
lack of data, is why the sample was N=29.

These tests pin the workaround: any window that comes back at the cap is bisected until
it fits, so completeness no longer depends on how busy KAP happened to be that month.
"""
from datetime import date

import pytest

from trailing_edge.scrapers.kap.client import LIST_RESULT_CAP, KapClient


class FakeKap:
    """Stands in for KAP: holds N disclosures per day, truncates to the NEWEST `cap`."""

    def __init__(self, per_day: dict[date, int], cap: int = LIST_RESULT_CAP):
        self.per_day = per_day
        self.cap = cap
        self.calls: list[tuple[date, date]] = []

    def query(self, frm: date, to: date) -> list[dict]:
        self.calls.append((frm, to))
        rows: list[dict] = []
        day = frm
        while day <= to:
            for i in range(self.per_day.get(day, 0)):
                rows.append(
                    {
                        "disclosureIndex": f"{day.isoformat()}-{i}",
                        "publishDate": day.isoformat(),
                        "subject": "Pay Alım Satım Bildirimi",
                        "disclosureClass": "DKB",
                    }
                )
            day = date.fromordinal(day.toordinal() + 1)
        rows.sort(key=lambda r: r["publishDate"])
        return rows[-self.cap:] if len(rows) > self.cap else rows  # newest kept


@pytest.fixture
def client(monkeypatch):
    c = KapClient.__new__(KapClient)  # bypass __init__/config
    c._filters = {
        "target_subject": "Pay Alım Satım Bildirimi",
        "target_class": "DKB",
    }
    return c


def _wire(client, fake: FakeKap):
    async def _raw(frm: date, to: date) -> list[dict]:
        return fake.query(frm, to)

    client._fetch_list_raw = _raw
    return client


@pytest.mark.asyncio
async def test_uncapped_range_is_returned_whole(client):
    fake = FakeKap({date(2024, 3, d): 10 for d in range(1, 32)})
    _wire(client, fake)

    got = await client.fetch_disclosure_list(date(2024, 3, 1), date(2024, 3, 31))

    assert len(got) == 310
    assert len(fake.calls) == 1  # no bisection needed


@pytest.mark.asyncio
async def test_a_capped_month_is_recovered_in_full(client):
    """The real failure: 31 days x 500/day = 15,500 rows, far over the 2,000 cap.

    A single monthly query would return only the last 4 days. Bisection must recover
    every one of the 15,500.
    """
    fake = FakeKap({date(2024, 3, d): 500 for d in range(1, 32)})
    _wire(client, fake)

    got = await client.fetch_disclosure_list(date(2024, 3, 1), date(2024, 3, 31))

    assert len(got) == 15_500
    days_seen = {r["publishDate"] for r in got}
    assert len(days_seen) == 31  # every day present, not just the tail
    assert "2024-03-01" in days_seen  # the head that KAP silently dropped
    assert len(fake.calls) > 1  # it did bisect


@pytest.mark.asyncio
async def test_naive_single_query_would_have_lost_the_head(client):
    """Demonstrates the bug this defends against, using the same fake."""
    fake = FakeKap({date(2024, 3, d): 500 for d in range(1, 32)})

    truncated = fake.query(date(2024, 3, 1), date(2024, 3, 31))

    assert len(truncated) == LIST_RESULT_CAP
    days = {r["publishDate"] for r in truncated}
    assert "2024-03-01" not in days  # head gone
    assert "2024-03-31" in days  # newest kept


@pytest.mark.asyncio
async def test_boundary_rows_are_not_double_counted(client):
    """Bisected halves are merged on disclosureIndex, so a row on the split boundary
    appears once, not twice."""
    fake = FakeKap({date(2024, 3, d): 500 for d in range(1, 32)})
    _wire(client, fake)

    got = await client.fetch_disclosure_list(date(2024, 3, 1), date(2024, 3, 31))

    ids = [r["disclosureIndex"] for r in got]
    assert len(ids) == len(set(ids))


@pytest.mark.asyncio
async def test_a_single_day_over_the_cap_is_reported_not_hidden(client, capsys):
    """A day denser than the cap cannot be narrowed by date. It must fail loudly."""
    fake = FakeKap({date(2024, 3, 1): LIST_RESULT_CAP + 50})
    _wire(client, fake)

    got = await client.fetch_disclosure_list(date(2024, 3, 1), date(2024, 3, 1))

    assert len(got) == LIST_RESULT_CAP  # still truncated - unavoidable
    # but the loss was surfaced, not swallowed (structlog writes to stdout)
    assert "kap_list_day_over_cap" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_non_dkb_disclosures_are_filtered_out(client):
    fake = FakeKap({date(2024, 3, 1): 5})
    _wire(client, fake)
    original = fake.query

    def with_noise(frm, to):
        rows = original(frm, to)
        rows.append(
            {
                "disclosureIndex": "noise-1",
                "publishDate": "2024-03-01",
                "subject": "Finansal Rapor",
                "disclosureClass": "FR",
            }
        )
        return rows

    fake.query = with_noise

    got = await client.fetch_disclosure_list(date(2024, 3, 1), date(2024, 3, 1))

    assert len(got) == 5
    assert all(r["disclosureClass"] == "DKB" for r in got)
