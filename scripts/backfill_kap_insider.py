"""
Backfill KAP insider transactions.

Default start is 2015-01-01, not because more history is nicer to have but because the
sample has to clear the power gate: distinguishing a 55% hit rate from a 50% null at
alpha=0.05 with 80% power needs ~784 cluster events, and below that `compute_base_rate`
correctly refuses to return a verdict. Measured DKB volume is ~22 disclosures/week
(~1,100/year), so roughly a decade of history is what the gate costs.

Monthly chunking is safe as of the client's cap-aware bisection: KAP's list endpoint
truncates at 2,000 rows, keeps the NEWEST, and silently drops the older head of the
range - a monthly query used to return only the last ~week of that month. See
`scrapers/kap/client.py::_fetch_list_complete`. Chunks are still monthly here because
that is the unit the resume ledger (ScraperRun.metadata_) is keyed on; the client
subdivides beneath it as needed.

Usage:
    uv run scripts/backfill_kap_insider.py
    uv run scripts/backfill_kap_insider.py --from 2020-01-01
    uv run scripts/backfill_kap_insider.py --dry-run
"""
import asyncio
import calendar
import sys
from datetime import date

import click
import httpx

sys.path.insert(0, "src")

from trailing_edge.core.db import init_db
from trailing_edge.core.logging import configure_logging, get_logger
from trailing_edge.scrapers.kap.insider import KapInsiderScraper

_log = get_logger(__name__)

# 2016-06 is the earliest date this pipeline can actually parse, not a preference.
#
# KAP's insider filings changed format mid-2016. Measured disclosureType by month:
#   2016-03 DUY=57   2016-04 DUY=53   2016-05 DUY=138
#   2016-06 ODA=111  2016-07 ODA=50   2016-08 ODA=108
#
# DUY-era filings are a free-form PDF the insider mailed in ("...açıklama ekte yer
# almaktadır", attachment "nthol2.pdf") - often a scan, with no structured table.
# parse_dkb_transactions expects KAP's ODA form and extracts nothing from them: a probe
# run over 2015 stored 50 disclosures and produced ZERO transactions, silently.
#
# Starting earlier than this costs hours of PDF downloads and yields no usable rows.
# Reading DUY-era filings at all would need OCR plus a free-form extractor - a separate
# project, not a parameter.
#
# ~1,100 DKB disclosures/year measured, so 2016-06 → today is ~11,000 filings: well past
# the ~784 events the power gate in signals/base_rate.py requires.
DEFAULT_START = date(2016, 6, 1)

# WAF backoff, applied AFTER a block - never before one.
#
# This used to be a list of (pre_sleep, label) pairs that the retry loop slept through
# *before every attempt, including the first*. So every chunk paid a 60s toll even when
# nothing had gone wrong: over 122 months that is ~2 hours of the run spent asleep on the
# happy path. That is not WAF protection, it is a tax on success.
#
# The escalation ladder itself is kept intact - if KAP's WAF does disconnect the warmup
# GET (httpx.RemoteProtocolError = IP throttled), back off 1 min, then 10, then 20. The
# protection is now reactive, which is the only thing a backoff can usefully be.
_WAF_BACKOFF_S = [60, 600, 1200]

# A courtesy gap between chunks so a long backfill does not arrive as one unbroken
# burst. Small enough to be irrelevant to the runtime (122 x 2s = ~4 min), unlike the
# 60s it replaces.
_CHUNK_GAP_S = 2


def generate_monthly_chunks(start: date, end: date) -> list[tuple[date, date]]:
    chunks = []
    year, month = start.year, start.month
    while True:
        from_date = date(year, month, 1)
        if from_date > end:
            break
        last_day = calendar.monthrange(year, month)[1]
        to_date = min(date(year, month, last_day), end)
        chunks.append((from_date, to_date))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return chunks


async def get_completed_chunks() -> set[tuple[date, date]]:
    from sqlalchemy import select

    from trailing_edge.core.db import get_session
    from trailing_edge.models.kap import ScraperRun

    async with get_session() as session:
        stmt = select(ScraperRun.metadata_).where(
            ScraperRun.status == "SUCCESS",
            ScraperRun.metadata_["backfill"].astext == "true",
        )
        rows = (await session.execute(stmt)).scalars().all()

    completed: set[tuple[date, date]] = set()
    for meta in rows:
        if meta and meta.get("from_date") and meta.get("to_date"):
            completed.add(
                (
                    date.fromisoformat(meta["from_date"]),
                    date.fromisoformat(meta["to_date"]),
                )
            )
    return completed


async def _run_chunk(from_date: date, to_date: date) -> None:
    """Run one chunk, backing off only if the WAF actually blocks us."""
    last_exc: Exception | None = None

    for attempt in range(len(_WAF_BACKOFF_S) + 1):
        if attempt:
            backoff = _WAF_BACKOFF_S[attempt - 1]
            _log.warning(
                "chunk_waf_backoff",
                from_date=from_date,
                to_date=to_date,
                attempt=attempt + 1,
                sleep_s=backoff,
            )
            await asyncio.sleep(backoff)

        _log.info(
            "chunk_start", from_date=from_date, to_date=to_date, attempt=attempt + 1
        )
        try:
            scraper = KapInsiderScraper(backfill=True)
            result = await scraper.run(from_date, to_date)
            _log.info(
                "chunk_done",
                from_date=from_date,
                to_date=to_date,
                seen=result.records_seen,
                inserted=result.records_inserted,
                skipped=result.records_skipped,
            )
            return
        except httpx.RemoteProtocolError as exc:
            # Warmup GET disconnected -> KAP's WAF has IP-throttled us.
            last_exc = exc
            _log.warning(
                "chunk_waf_blocked",
                from_date=from_date,
                to_date=to_date,
                attempt=attempt + 1,
                error=str(exc),
            )

    raise RuntimeError(
        f"Chunk {from_date}-{to_date} failed after {len(_WAF_BACKOFF_S)} WAF backoffs"
    ) from last_exc


async def main_async(
    start: date,
    dry_run: bool,
    forced: frozenset[tuple[int, int]] = frozenset(),
) -> None:
    await init_db()

    end = date.today()
    chunks = generate_monthly_chunks(start, end)
    completed = await get_completed_chunks()

    _log.info(
        "backfill_start",
        total_chunks=len(chunks),
        already_done=len(completed),
        dry_run=dry_run,
        forced_months=len(forced),
    )

    for from_date, to_date in chunks:
        is_forced = (from_date.year, from_date.month) in forced
        if (from_date, to_date) in completed and not is_forced:
            _log.info(
                "chunk_skip",
                from_date=from_date,
                to_date=to_date,
                reason="already_ingested",
            )
            continue

        if dry_run:
            _log.info("chunk_dry_run", from_date=from_date, to_date=to_date, forced=is_forced)
            continue

        await _run_chunk(from_date, to_date)
        await asyncio.sleep(_CHUNK_GAP_S)

    _log.info("backfill_complete", dry_run=dry_run)


@click.command()
@click.option(
    "--from",
    "from_date",
    default=str(DEFAULT_START),
    help=f"Start date YYYY-MM-DD (default: {DEFAULT_START})",
)
@click.option("--dry-run", is_flag=True, help="List chunks without executing")
@click.option(
    "--force-months",
    "force_months",
    default="",
    help="Comma-separated YYYY-MM values to re-run regardless of completion (e.g. 2025-07,2025-10)",
)
def main(from_date: str, dry_run: bool, force_months: str) -> None:
    configure_logging()
    forced: set[tuple[int, int]] = set()
    if force_months:
        for m in force_months.split(","):
            y, mo = m.strip().split("-")
            forced.add((int(y), int(mo)))
    asyncio.run(main_async(date.fromisoformat(from_date), dry_run, frozenset(forced)))


if __name__ == "__main__":
    main()
