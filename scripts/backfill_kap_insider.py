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

# 2015-01 is where the documents stop having a table to read, measured - not a preference.
#
# An earlier version of this comment blamed the DUY/ODA disclosureType split and set the
# floor at 2016-06. That was wrong, and worth recording because the mistake was a
# confound: a 2015 backfill did produce zero transactions, but the date-separator bug in
# parser._DATE_RE would have zeroed those years regardless of their format. Two causes,
# one symptom. Re-probing with the fixed parser separates them:
#
#   year  docs with a table   parsed   type
#   2013         0/3            0%     DUY   <- genuinely no table in the document
#   2014         0/3            0%     DUY   <- genuinely no table
#   2015         3/3          100%     DUY   <- parses fine; DUY was never the problem
#   2016         1/3           33%     ODA
#
# So the format label was a red herring: DUY-era 2015 filings carry the same structured
# table as a modern one. What actually ends is the table itself - 2013/2014 filings are
# a free-form PDF the insider mailed in ("...açıklama ekte yer almaktadır", attachment
# "nthol2.pdf"), often a scan, with nothing to extract. Reading those would need OCR plus
# a free-form extractor: a separate project, not a parameter.
#
# If some early-2015 filings do turn out to be table-less, they now surface as
# dkb_yielded_no_transactions rather than vanishing silently. The floor is a measured
# claim with a safety net under it, not an assumption.
#
# ~1,100 DKB filings/year measured, so 2015-01 → today is ~12,500 filings: well past the
# ~784 events the power gate in signals/base_rate.py requires, and long enough to test
# regime-conditionally rather than pooling a decade into one number.
DEFAULT_START = date(2015, 1, 1)

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

# After the archive has been swept once, re-sweep whatever the WAF cost us. Silence is
# the only thing that measurably clears KAP's throttle - 40 minutes of it took the block
# rate from 61% to 0%, while cutting the request rate did nothing - so each pass opens
# with a real pause rather than trickling straight back in.
_RECOVERY_PASSES = 4
_RECOVERY_PAUSE_S = 1800  # 30 minutes

# Hard ceiling on one month. A chunk is bounded work - one list call, then two requests
# per filing at a few per second - so anything past this is not slow, it is stuck.
#
# This is a precaution, not a fix for an observed hang. It was added on the strength of
# a run that appeared to sit silent for three hours; it had not. structlog timestamps in
# UTC and the machine clock is UTC+3, so a healthy process looked frozen because the two
# numbers being compared were not in the same units. The process was working, and it was
# killed on a bad diagnosis - the same class of error this codebase spent the day
# catching, made by the person catching them.
#
# The deadline stays because a chunk that cannot finish in fifteen minutes is not doing
# useful work either way, and a run that can lose a month to a hang is better than one
# that can lose a night. But it is here as a guard against a hang that has never been
# observed, and this comment says so rather than implying a bug that was really a
# timezone.
_CHUNK_DEADLINE_S = 900  # 15 minutes


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


async def _chunks_with_status(status: str) -> set[tuple[date, date]]:
    from sqlalchemy import select

    from trailing_edge.core.db import get_session
    from trailing_edge.models.kap import ScraperRun

    async with get_session() as session:
        stmt = select(ScraperRun.metadata_).where(
            ScraperRun.status == status,
            ScraperRun.metadata_["backfill"].astext == "true",
        )
        rows = (await session.execute(stmt)).scalars().all()

    out: set[tuple[date, date]] = set()
    for meta in rows:
        if meta and meta.get("from_date") and meta.get("to_date"):
            out.add(
                (
                    date.fromisoformat(meta["from_date"]),
                    date.fromisoformat(meta["to_date"]),
                )
            )
    return out


async def get_completed_chunks() -> set[tuple[date, date]]:
    return await _chunks_with_status("SUCCESS")


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
            result = await asyncio.wait_for(
                scraper.run(from_date, to_date), timeout=_CHUNK_DEADLINE_S
            )
            _log.info(
                "chunk_done",
                from_date=from_date,
                to_date=to_date,
                seen=result.records_seen,
                inserted=result.records_inserted,
                skipped=result.records_skipped,
            )
            return
        except TimeoutError as exc:
            # Not slow - stuck. Abandon the month; the ledger keeps it PARTIAL and a
            # recovery pass will come back for it.
            last_exc = exc
            _log.error(
                "chunk_deadline_exceeded",
                from_date=from_date,
                to_date=to_date,
                attempt=attempt + 1,
                deadline_s=_CHUNK_DEADLINE_S,
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
    partial = await _chunks_with_status("PARTIAL")

    todo = [c for c in chunks if c not in completed or (c[0].year, c[0].month) in forced]

    # Untouched months FIRST, months that only need their WAF-dropped stragglers SECOND.
    #
    # A PARTIAL month is already 95% ingested - it just lost a handful of filings to a
    # WAF disconnect. An untouched month has nothing. Replaying the PARTIALs in calendar
    # order means re-listing and re-checking hundreds of already-stored filings before
    # any new history lands, and if the WAF drops a fresh straggler the month goes
    # PARTIAL again - so the archive can be swept repeatedly while the frontier never
    # moves. Fresh data first; the stragglers are cheap to collect at the end.
    fresh = [c for c in todo if c not in partial]
    stragglers = [c for c in todo if c in partial]
    ordered = fresh + stragglers

    _log.info(
        "backfill_start",
        total_chunks=len(chunks),
        already_done=len(completed),
        fresh=len(fresh),
        partial_retry=len(stragglers),
        dry_run=dry_run,
        forced_months=len(forced),
    )

    for from_date, to_date in ordered:
        if dry_run:
            _log.info("chunk_dry_run", from_date=from_date, to_date=to_date)
            continue

        await _run_chunk(from_date, to_date)
        await asyncio.sleep(_CHUNK_GAP_S)

    if dry_run:
        _log.info("backfill_complete", dry_run=True)
        return

    # Recovery passes.
    #
    # A month that lost disclosures to the WAF is PARTIAL, and the per-chunk cooldown no
    # longer waits for it - that wait cost half the runtime while the frontier stood
    # still. Instead the archive is swept once at speed, and then re-swept for exactly
    # what was missed. The ingest is idempotent, so a re-swept month re-fetches only its
    # lost disclosures and skips the hundreds already stored.
    #
    # A pause between passes is the one thing that actually clears KAP's throttle
    # (measured: 40 minutes of silence took the block rate from 61% to 0%, while slowing
    # the request rate did nothing). Passes stop when nothing is left, when a pass makes
    # no progress, or after the cap.
    for attempt in range(1, _RECOVERY_PASSES + 1):
        remaining = await _chunks_with_status("PARTIAL")
        remaining = [c for c in remaining if c in set(ordered)]
        if not remaining:
            break

        _log.info(
            "recovery_pass_start",
            attempt=attempt,
            months=len(remaining),
            pause_s=_RECOVERY_PAUSE_S,
        )
        await asyncio.sleep(_RECOVERY_PAUSE_S)

        for from_date, to_date in sorted(remaining):
            await _run_chunk(from_date, to_date)
            await asyncio.sleep(_CHUNK_GAP_S)

        still = [c for c in await _chunks_with_status("PARTIAL") if c in set(ordered)]
        _log.info(
            "recovery_pass_done",
            attempt=attempt,
            before=len(remaining),
            after=len(still),
        )
        if len(still) >= len(remaining):
            _log.warning("recovery_stalled", months=len(still))
            break

    left = [c for c in await _chunks_with_status("PARTIAL") if c in set(ordered)]
    if left:
        _log.warning("backfill_incomplete", partial_months=len(left))

    _log.info("backfill_complete", dry_run=False, partial_months=len(left))


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
