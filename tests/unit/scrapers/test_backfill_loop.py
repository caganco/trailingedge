"""The backfill loop must survive a month it cannot finish.

A chunk that exhausts its WAF backoffs raises RuntimeError. Its scraper_runs record is
already written (PARTIAL or FAILED), so a later sweep collects it - that is the ledger's
whole purpose. But the RuntimeError used to propagate out of the loop and abandon every
remaining month behind the bad one. This was harmless while the backoff ladder ended at 20
minutes (a chunk almost never exhausted); shortening the ladder to 60/120/240s made
exhaustion common, and one blocked month began killing the entire run.
"""
import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "backfill_kap_insider.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("backfill_kap_insider", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["backfill_kap_insider"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_a_chunk_that_raises_does_not_abandon_the_months_behind_it(monkeypatch):
    mod = _load_module()

    months = [
        (date(2015, 1, 1), date(2015, 1, 31)),
        (date(2015, 2, 1), date(2015, 2, 28)),
        (date(2015, 3, 1), date(2015, 3, 31)),
    ]

    async def _noop():
        return None

    monkeypatch.setattr(mod, "init_db", _noop)
    monkeypatch.setattr(mod, "generate_monthly_chunks", lambda s, e: months)
    monkeypatch.setattr(mod, "get_completed_chunks", lambda: _empty_set())
    monkeypatch.setattr(mod, "_chunks_with_status", lambda status: _empty_set())
    monkeypatch.setattr(mod.asyncio, "sleep", _noop_arg)

    attempted: list[tuple] = []

    async def fake_run_chunk(frm, to):
        attempted.append((frm, to))
        if frm == date(2015, 1, 1):
            # exhausted WAF backoffs
            raise RuntimeError("Chunk 2015-01 failed after 3 WAF backoffs")
        if frm == date(2015, 2, 1):
            # a dead proxy: this is the class that used to escape the RuntimeError-only catch
            import httpx

            raise httpx.ProxyError("402 Payment Required")

    monkeypatch.setattr(mod, "_run_chunk", fake_run_chunk)

    await mod.main_async(date(2015, 1, 1), dry_run=False)

    # every month was attempted - neither the RuntimeError nor the ProxyError abandoned March
    assert attempted == months


async def _empty_set():
    return set()


async def _noop_arg(*a, **k):
    return None
