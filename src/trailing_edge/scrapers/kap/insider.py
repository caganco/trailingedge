"""KAP insider scraper orchestrator."""
import asyncio
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy import select

from trailing_edge.core.db import get_session
from trailing_edge.core.http import RateLimitedClient
from trailing_edge.core.logging import get_logger
from trailing_edge.models.kap import KapDisclosure, ScraperRun
from trailing_edge.scrapers.base import AbstractScraper
from trailing_edge.scrapers.kap.client import KapClient
from trailing_edge.scrapers.kap.parser import (
    parse_disclosure_metadata,
    parse_dkb_transactions,
    parse_oda_transactions,
)
from trailing_edge.storage.repository import KapRepository

_log = get_logger(__name__)

SCRAPER_NAME = "kap_insider"

# DKB PDFs that parsed to zero transactions land here (gitignored under /reports),
# named by disclosure id, so parse failures keep their evidence for offline
# diagnosis and a later bulk repair pass.
_QUARANTINE_DIR = Path("reports/parse_failures")

# One short pause before re-attempting the disclosures KAP's WAF disconnected on.
#
# This was an escalating ladder (90s, 4min, 10min) that recovered a month fully before
# moving on. It worked, and it was the wrong place to spend the time: measured over 8
# chunks, 52% of the entire run was spent asleep inside it - 7.1 minutes per month, some
# 14 hours across the archive - while the frontier stood still.
#
# The ladder is unnecessary because a blocked request is now nearly free.
# RemoteProtocolError fails fast (core/http._is_retryable), so a chunk can take whatever
# the WAF lets through, defer the rest, and finish PARTIAL. The resume ledger counts only
# SUCCESS as done, so a later pass comes back for exactly the missed disclosures - and the
# ingest is idempotent, so that re-fetch costs only what was actually lost. Recovery
# belongs in a second pass over the archive, not in a sleep inside every month.
#
# One 90s round is kept: it is cheap and clears roughly half the deferrals immediately
# (measured 76 -> 34), which keeps the PARTIAL set small enough for the recovery pass.
_WAF_COOLDOWNS_S = (90,)


@dataclass
class ScraperRunResult:
    records_seen: int
    records_inserted: int
    records_updated: int
    records_skipped: int
    status: str


@dataclass
class _Counts:
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    empty_dkb: int = 0


class KapInsiderScraper(AbstractScraper[ScraperRunResult]):
    def __init__(self, *, backfill: bool = False) -> None:
        self._backfill = backfill

    async def _already_stored(self, ids: list[str]) -> set[str]:
        """Which of these disclosure ids are already in the database.

        One query for the whole month. Asking per-disclosure opened a fresh session
        for each of ~200 filings, which is most of the cost of re-running an
        already-ingested month - and PARTIAL months get re-run by design.
        """
        if not ids:
            return set()
        async with get_session() as session:
            rows = await session.execute(
                select(KapDisclosure.kap_disclosure_id).where(
                    KapDisclosure.kap_disclosure_id.in_(ids)
                )
            )
            return set(rows.scalars().all())

    async def _ingest_one(
        self,
        kap: KapClient,
        disc: dict,
        counts: _Counts,
        already_stored: set[str],
    ) -> None:
        """Fetch, parse and store one disclosure.

        Transport failures propagate: the caller decides whether to retry or defer.
        Swallowing them here is what silently lost 12% of a backfill.
        """
        kap_disclosure_id = str(disc.get("disclosureIndex", ""))
        is_correction = bool(disc.get("isChanged") or disc.get("isCorrection") or False)
        already_exists = kap_disclosure_id in already_stored

        if already_exists and not is_correction:
            counts.skipped += 1
            _log.debug("disclosure_skipped", kap_disclosure_id=kap_disclosure_id)
            return

        detail = await kap.fetch_disclosure_detail(kap_disclosure_id)
        # Pass list_item so metadata uses DKB class (not detail's DUY)
        dto = parse_disclosure_metadata(detail, list_item=disc)

        txs = []
        pdf_bytes: bytes | None = None
        # Route by the list API's disclosureClass (reliable DKB indicator)
        is_dkb = disc.get("disclosureClass") == "DKB"
        if is_dkb:
            attachments = detail.get("attachments", [])
            if attachments:
                obj_id = attachments[0].get("objId", "")
                if obj_id:
                    pdf_bytes = await kap.fetch_pdf(obj_id)
                    txs = parse_dkb_transactions(
                        pdf_bytes,
                        ticker=dto.ticker,
                        insider_name="",
                        published_on=dto.published_at.date() if dto.published_at else None,
                    )
        else:
            body = detail.get("disclosureBody", "") or ""
            txs = parse_oda_transactions(body, ticker=dto.ticker)

        # A DKB disclosure that yields no transactions is a parse failure, not an
        # empty filing - the whole point of a Pay Alim Satim Bildirimi is that it
        # reports at least one trade. Surface it, and QUARANTINE the PDF: the bytes
        # were already paid for, and a failure that keeps its evidence can be
        # diagnosed offline and repaired in bulk, instead of re-fought through the
        # WAF one probe at a time.
        if is_dkb and not txs:
            counts.empty_dkb += 1
            quarantined = None
            if pdf_bytes:
                _QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
                quarantined = _QUARANTINE_DIR / f"{kap_disclosure_id}.pdf"
                quarantined.write_bytes(pdf_bytes)
            _log.warning(
                "dkb_yielded_no_transactions",
                kap_disclosure_id=kap_disclosure_id,
                ticker=dto.ticker,
                disclosure_type=disc.get("disclosureType"),
                published_at=str(dto.published_at),
                quarantined=str(quarantined) if quarantined else None,
            )

        async with get_session() as session:
            repo = KapRepository(session)
            model, created = await repo.upsert_disclosure(dto)
            result = await repo.upsert_transactions(model.id, txs)

        if created:
            counts.inserted += 1
        else:
            counts.updated += 1
        counts.inserted += result.inserted

        _log.info(
            "disclosure_processed",
            kap_disclosure_id=kap_disclosure_id,
            ticker=dto.ticker,
            created=created,
            tx_inserted=result.inserted,
        )

    async def run(self, from_date: date, to_date: date) -> ScraperRunResult:
        # Create the audit run record
        run_meta = (
            {"backfill": True, "from_date": from_date.isoformat(), "to_date": to_date.isoformat()}
            if self._backfill else None
        )
        async with get_session() as session:
            repo = KapRepository(session)
            run = await repo.create_scraper_run(SCRAPER_NAME, metadata=run_meta)
        run_id: int = run.id

        counts = _Counts()
        seen = 0
        lost = 0
        error_msg: str | None = None

        try:
            async with RateLimitedClient() as http:
                kap = KapClient(http)
                await kap.warmup()
                disclosures = await kap.fetch_disclosure_list(from_date, to_date)
                seen = len(disclosures)
                already_stored = await self._already_stored(
                    [str(d.get("disclosureIndex", "")) for d in disclosures]
                )

                # KAP's WAF intermittently disconnects mid-request. Those failures used
                # to be logged and skipped, dropping the disclosure outright - 12% of
                # them on a real backfill, silently, while the run still said SUCCESS.
                # A disconnect is transient, so a dropped disclosure is retried once
                # after a cooldown; anything still missing downgrades the run to PARTIAL.
                deferred: list[dict] = []
                for disc in disclosures:
                    try:
                        await self._ingest_one(kap, disc, counts, already_stored)
                    except Exception as exc:
                        deferred.append(disc)
                        _log.warning(
                            "disclosure_deferred",
                            kap_disclosure_id=str(disc.get("disclosureIndex", "")),
                            error=str(exc),
                        )

                for attempt, cooldown in enumerate(_WAF_COOLDOWNS_S, start=1):
                    if not deferred:
                        break
                    _log.info(
                        "waf_cooldown",
                        attempt=attempt,
                        deferred=len(deferred),
                        sleep_s=cooldown,
                    )
                    await asyncio.sleep(cooldown)
                    retrying, deferred = deferred, []
                    for disc in retrying:
                        try:
                            await self._ingest_one(kap, disc, counts, already_stored)
                        except Exception as exc:
                            deferred.append(disc)
                            last = attempt == len(_WAF_COOLDOWNS_S)
                            _log.log(
                                40 if last else 30,  # ERROR on the final round, else WARNING
                                "disclosure_error" if last else "disclosure_deferred",
                                kap_disclosure_id=str(disc.get("disclosureIndex", "")),
                                attempt=attempt,
                                error=str(exc),
                            )
                lost = len(deferred)

        except Exception as exc:
            error_msg = str(exc)
            _log.error("scraper_failed", error=error_msg, exc_info=True)
            async with get_session() as session:
                run_obj = await session.get(ScraperRun, run_id)
                if run_obj:
                    repo = KapRepository(session)
                    await repo.finish_scraper_run(
                        run_obj,
                        status="FAILED",
                        records_seen=seen,
                        records_inserted=counts.inserted,
                        records_updated=counts.updated,
                        records_skipped=counts.skipped,
                        error_message=error_msg,
                    )
            raise

        # A month that lost even one disclosure is not a SUCCESS. PARTIAL keeps it out
        # of the resume ledger's completed set so a later pass comes back for it; the
        # ingest is idempotent (disclosure_exists skips what is already stored), so the
        # re-run costs only the disclosures that were actually missed.
        status = "SUCCESS" if lost == 0 else "PARTIAL"
        async with get_session() as session:
            run_obj = await session.get(ScraperRun, run_id)
            if run_obj:
                repo = KapRepository(session)
                await repo.finish_scraper_run(
                    run_obj,
                    status=status,
                    records_seen=seen,
                    records_inserted=counts.inserted,
                    records_updated=counts.updated,
                    records_skipped=counts.skipped,
                    error_message=f"{lost} disclosures unrecovered" if lost else None,
                )

        if lost:
            _log.warning("chunk_incomplete", seen=seen, lost=lost, status=status)

        # A run that stored disclosures but extracted no transactions from most of them
        # is a failed run wearing a SUCCESS label. Surface the ratio, not just the counts.
        if counts.empty_dkb:
            _log.warning(
                "dkb_parse_yield_low" if counts.empty_dkb * 2 >= seen else "dkb_parse_partial",
                seen=seen,
                empty_dkb=counts.empty_dkb,
                empty_pct=round(counts.empty_dkb / seen * 100, 1) if seen else 0.0,
            )

        _log.info(
            "scraper_done",
            seen=seen,
            inserted=counts.inserted,
            updated=counts.updated,
            skipped=counts.skipped,
            empty_dkb=counts.empty_dkb,
            lost=lost,
        )
        return ScraperRunResult(
            records_seen=seen,
            records_inserted=counts.inserted,
            records_updated=counts.updated,
            records_skipped=counts.skipped,
            status=status,
        )
