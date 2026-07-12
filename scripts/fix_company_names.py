"""D1 fix: update companies.company_name from known_companies.yaml.

KAP scraper companyTitle field returns the generic platform title
("KAMUYU AYDINLATMA PLATFORMU") instead of the real company name.
This script applies a static correction from config/known_companies.yaml.
Idempotent - safe to re-run.

NOTE (2026-05-30): The known_companies override is now integrated into
seed_graph_from_insider_tx.py. Running seed after a DB reseed automatically
applies correct company names without this script. This script remains
available as a one-off repair tool but is no longer part of the required
pipeline.
"""
import asyncio
import sys
from pathlib import Path

import yaml
from sqlalchemy import text

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

YAML_FILE = ROOT / "config" / "known_companies.yaml"


async def main() -> None:
    from trailing_edge.core.logging import configure_logging, get_logger

    configure_logging()
    log = get_logger("fix_company_names")

    with open(YAML_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    companies = data.get("companies", {})

    if not companies:
        log.warning("no_entries_in_yaml")
        return

    from trailing_edge.core.db import _get_engine
    _get_engine()
    from trailing_edge.core.db import _engine
    assert _engine is not None

    async with _engine.begin() as conn:
        for ticker, real_name in companies.items():
            row = (await conn.execute(
                text("SELECT id, company_name FROM companies WHERE ticker = :t"),
                {"t": ticker},
            )).one_or_none()
            if row is None:
                log.warning("ticker_not_in_db", ticker=ticker)
                continue
            if row.company_name == real_name:
                log.info("already_correct", ticker=ticker, name=real_name)
                continue
            await conn.execute(
                text("UPDATE companies SET company_name = :name WHERE ticker = :t"),
                {"name": real_name, "t": ticker},
            )
            log.info("company_name_updated", ticker=ticker, old=row.company_name, new=real_name)
    # engine.begin() commits on clean exit, rolls back on exception

    print("Done. Verify:")
    async with _engine.connect() as conn:
        rows = (await conn.execute(
            text(
                "SELECT ticker, company_name FROM companies"
                " WHERE ticker IN ('KAPLM','RALYH','KUYAS','MACKO','OZYSR')"
                " ORDER BY ticker"
            )
        )).all()
        for r in rows:
            print(f"  {r.ticker} | {r.company_name}")


if __name__ == "__main__":
    asyncio.run(main())
