"""KAP insider scraper orchestrator."""
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from trailing_edge.core.db import get_session
from trailing_edge.core.http import RateLimitedClient
from trailing_edge.core.logging import get_logger
from trailing_edge.models.kap import ScraperRun
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


@dataclass
class ScraperRunResult:
    records_seen: int
    records_inserted: int
    records_updated: int
    records_skipped: int
    status: str


class KapInsiderScraper(AbstractScraper[ScraperRunResult]):
    def __init__(self, *, backfill: bool = False) -> None:
        self._backfill = backfill

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

        seen = inserted = updated = skipped = 0
        empty_dkb = 0  # DKB filings that parsed to zero transactions - see below
        error_msg: str | None = None

        try:
            async with RateLimitedClient() as http:
                kap = KapClient(http)
                await kap.warmup()
                disclosures = await kap.fetch_disclosure_list(from_date, to_date)
                seen = len(disclosures)

                for disc in disclosures:
                    disclosure_index = str(disc.get("disclosureIndex", ""))
                    # Use disclosureIndex as the stable ID (list API has no disclosureId)
                    kap_disclosure_id = disclosure_index
                    is_correction = bool(disc.get("isChanged") or disc.get("isCorrection") or False)

                    async with get_session() as session:
                        repo = KapRepository(session)
                        already_exists = await repo.disclosure_exists(kap_disclosure_id)

                    if already_exists and not is_correction:
                        skipped += 1
                        _log.debug("disclosure_skipped", kap_disclosure_id=kap_disclosure_id)
                        continue

                    try:
                        detail = await kap.fetch_disclosure_detail(disclosure_index)
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
                                    )
                        else:
                            body = detail.get("disclosureBody", "") or ""
                            txs = parse_oda_transactions(body, ticker=dto.ticker)

                        # A DKB disclosure that yields no transactions is a parse failure,
                        # not an empty filing - the whole point of a Pay Alim Satim
                        # Bildirimi is that it reports at least one trade. Surface it,
                        # and QUARANTINE the PDF: the bytes were already paid for, and
                        # a failure that keeps its evidence can be diagnosed offline and
                        # repaired in bulk later, instead of re-fought through the WAF
                        # one probe at a time.
                        if is_dkb and not txs:
                            empty_dkb += 1
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
                            inserted += 1
                        else:
                            updated += 1
                        inserted += result.inserted

                        _log.info(
                            "disclosure_processed",
                            kap_disclosure_id=kap_disclosure_id,
                            ticker=dto.ticker,
                            created=created,
                            tx_inserted=result.inserted,
                        )

                    except Exception as exc:
                        _log.error(
                            "disclosure_error",
                            kap_disclosure_id=kap_disclosure_id,
                            error=str(exc),
                            exc_info=True,
                        )

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
                        records_inserted=inserted,
                        records_updated=updated,
                        records_skipped=skipped,
                        error_message=error_msg,
                    )
            raise

        async with get_session() as session:
            run_obj = await session.get(ScraperRun, run_id)
            if run_obj:
                repo = KapRepository(session)
                await repo.finish_scraper_run(
                    run_obj,
                    status="SUCCESS",
                    records_seen=seen,
                    records_inserted=inserted,
                    records_updated=updated,
                    records_skipped=skipped,
                )

        # A run that stored disclosures but extracted no transactions from most of them
        # is a failed run wearing a SUCCESS label. Surface the ratio, not just the counts.
        if empty_dkb:
            _log.warning(
                "dkb_parse_yield_low" if empty_dkb * 2 >= seen else "dkb_parse_partial",
                seen=seen,
                empty_dkb=empty_dkb,
                empty_pct=round(empty_dkb / seen * 100, 1) if seen else 0.0,
            )

        _log.info(
            "scraper_done",
            seen=seen,
            inserted=inserted,
            updated=updated,
            skipped=skipped,
            empty_dkb=empty_dkb,
        )
        return ScraperRunResult(
            records_seen=seen,
            records_inserted=inserted,
            records_updated=updated,
            records_skipped=skipped,
            status="SUCCESS",
        )
