"""Re-parse quarantined DKB PDFs with the current parser and store what now succeeds.

The ingest quarantines every DKB PDF that parses to zero transactions
(reports/parse_failures/{disclosure_id}.pdf). After a parser improvement, this script
replays those files offline - no network, no WAF - and upserts the recovered
transactions against their already-stored disclosures. Files that still fail stay in
quarantine for the next diagnosis round; files that succeed are removed.

Usage:
    uv run python scripts/reparse_quarantine.py           # repair
    uv run python scripts/reparse_quarantine.py --dry-run # report only
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click

sys.path.insert(0, "src")

from sqlalchemy import select  # noqa: E402

from trailing_edge.core.db import get_session, init_db  # noqa: E402
from trailing_edge.core.logging import configure_logging, get_logger  # noqa: E402
from trailing_edge.models.kap import KapDisclosure  # noqa: E402
from trailing_edge.scrapers.kap.parser import parse_dkb_transactions  # noqa: E402
from trailing_edge.storage.repository import KapRepository  # noqa: E402

_log = get_logger(__name__)

QUARANTINE_DIR = Path("reports/parse_failures")


async def main_async(dry_run: bool) -> None:
    await init_db()

    pdfs = sorted(QUARANTINE_DIR.glob("*.pdf"))
    if not pdfs:
        click.echo("Quarantine is empty - nothing to repair.")
        return

    recovered = still_failing = orphaned = tx_total = 0

    for pdf_path in pdfs:
        disclosure_id = pdf_path.stem

        async with get_session() as session:
            row = (
                await session.execute(
                    select(KapDisclosure).where(
                        KapDisclosure.kap_disclosure_id == disclosure_id
                    )
                )
            ).scalar_one_or_none()

            if row is None:
                # PDF outlived its disclosure row (e.g. the DB was reset since
                # quarantine). Leave the file; a future backfill will re-store the
                # disclosure and the next repair pass will pick it up.
                orphaned += 1
                continue

            txs = parse_dkb_transactions(pdf_path.read_bytes(), ticker=row.ticker)
            if not txs:
                still_failing += 1
                continue

            inserted = 0
            if not dry_run:
                repo = KapRepository(session)
                result = await repo.upsert_transactions(row.id, txs)
                inserted = result.inserted

        recovered += 1
        tx_total += len(txs)
        _log.info(
            "quarantine_repaired",
            kap_disclosure_id=disclosure_id,
            ticker=row.ticker,
            transactions=len(txs),
            inserted=inserted,
            dry_run=dry_run,
        )
        if not dry_run:
            pdf_path.unlink()

    click.echo(
        f"Quarantine: {len(pdfs)} files | repaired {recovered} "
        f"({tx_total} transactions) | still failing {still_failing} | orphaned {orphaned}"
        + ("  [DRY RUN - nothing written or deleted]" if dry_run else "")
    )


@click.command()
@click.option("--dry-run", is_flag=True, help="Parse and report without writing.")
def main(dry_run: bool) -> None:
    configure_logging()
    asyncio.run(main_async(dry_run))


if __name__ == "__main__":
    main()
