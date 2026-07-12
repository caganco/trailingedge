"""Load Borsa Istanbul's official end-of-day bulletin into price_history.

Why this exists
---------------
yfinance is survivorship-contaminated for BIST. It serves nothing for a delisted
ticker - not even the years it was actively trading - so a 2015-2017 study loses
every company that died afterwards. Measured on this dataset: 31% of insider
clusters had no price data, and the names behind them (MCTAS, MENBA, OZBAL,
IZTAR...) are exactly the small caps that went to zero. That deletion is not
random; it removes the worst outcomes and manufactures an edge. compute_base_rate
returns SURVIVORSHIP_BIASED for precisely this reason.

The exchange's own bulletin (PP_GUNSONUFIYATHACIM, "Pay Piyasasi Gun Sonu Fiyat
Hacim") is survivorship-clean by construction: it records what actually traded each
day, so a company that later delisted is still in the file for every day it lived.
One 2016 month carries 420 distinct equities against yfinance's 185 for the whole
universe, and 8 of the 10 tickers yfinance could not price are present with a full
month of closes.

Notes
-----
- ZIPs are read in place, never extracted: the archive is ~1.8 GB and the disk has
  little headroom.
- The bulletin also carries BRUT TAKAS (gross settlement) and GECICI DURDURMA
  (suspended) - the VBTS tradability flags the pipeline currently lacks. Not loaded
  yet; recorded here as the obvious next use of this source.
Corporate actions
-----------------
The bulletin quotes RAW traded prices, so a bonus issue or dividend drop looks like a
loss: a 100% bedelsiz halves the printed close overnight, and a naive return over that
window reads -50%. There is no adjustment file in the archive (corporate_actions/ is
empty), but the bulletin adjusts one field itself - ONCEKI KAPANIS FIYATI, the previous
close as the exchange restates it. Where a corporate action occurred, that value is the
ADJUSTED prior close, so

    close(t) / previous_close_as_recorded(t)

is the true one-day total return, dividends and bonus issues already netted out.
Measured over 2016-06..2016-08: 30 of 24,967 day-pairs disagree with the actual prior
close (0.12%), with factors like 0.953 (ANHYT) and 0.908 (BOSSA) - exactly the dividend
drops it should catch.

This loader therefore stores a chained TOTAL-RETURN series, not the raw print:

    A(first) = close(first)
    A(t)     = A(t-1) * close(t) / previous_close_as_recorded(t)

so (A(exit) / A(entry) - 1) is a correct cumulative return across any corporate action.
The stored value is an index level, not the traded price - which is what the return
math needs, and the only thing it is used for.

Usage:
    uv run python scripts/load_official_prices.py --from 2015-01
"""
from __future__ import annotations

import asyncio
import csv
import io
import sys
import zipfile
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import click

sys.path.insert(0, "src")

from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from trailing_edge.core.db import get_session, init_db  # noqa: E402
from trailing_edge.core.logging import configure_logging, get_logger  # noqa: E402
from trailing_edge.models.signal import PriceHistory  # noqa: E402

_log = get_logger(__name__)

ARCHIVE = Path(
    r"C:\Users\cagan\bist-trading-system\data\bist_datastore_archive\prices_official"
)

# Column positions in the bulletin (0-based), verified against the 2016-06 header.
C_DATE = 0
C_CODE = 1
C_TYPE = 7
C_PREV_CLOSE = 16  # ONCEKI KAPANIS FIYATI - restated by the exchange across a CA
C_OPEN = 17
C_LOW = 20
C_HIGH = 21
C_CLOSE = 22
C_VOLUME = 29

# Ignore adjustment factors within this band: they are rounding in the printed
# previous close, not a corporate action.
_CA_EPS = Decimal("0.005")

# Equities only. The bulletin also lists warrants, ETFs, rights and so on.
EQUITY_TYPE = "MSPOTEQT"


def _dec(raw: str) -> Decimal | None:
    raw = raw.strip()
    if not raw or raw in {"0", "0.0", "0.00"}:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _rows_from_csv(text: str) -> list[dict]:
    reader = csv.reader(io.StringIO(text), delimiter=";")
    out: list[dict] = []
    for parts in reader:
        if len(parts) <= C_VOLUME:
            continue  # header lines (Turkish + English) and any short row
        if parts[C_TYPE].strip() != EQUITY_TYPE:
            continue
        try:
            price_date = datetime.strptime(parts[C_DATE].strip(), "%Y-%m-%d").date()
        except ValueError:
            continue

        close = _dec(parts[C_CLOSE])
        if close is None or close <= 0:
            continue  # a day with no trade has no close to measure against

        # "ACSEL.E" -> "ACSEL". The suffix marks the instrument series, not the company.
        ticker = parts[C_CODE].strip().split(".")[0].upper()
        if not ticker:
            continue

        volume_raw = parts[C_VOLUME].strip()
        try:
            volume = int(float(volume_raw)) if volume_raw else None
        except ValueError:
            volume = None

        out.append(
            {
                "ticker": ticker,
                "price_date": price_date,
                "open_try": _dec(parts[C_OPEN]),
                "high_try": _dec(parts[C_HIGH]),
                "low_try": _dec(parts[C_LOW]),
                "close_try": close,
                "volume": volume,
                "_prev_close": _dec(parts[C_PREV_CLOSE]),
            }
        )
    return out


def chain_total_return(rows: list[dict]) -> list[dict]:
    """Replace raw closes with a chained total-return index, per ticker.

    A raw close series is wrong across a corporate action - a bonus issue halves the
    print and a naive window return reads it as a 50% loss. The exchange restates
    ONCEKI KAPANIS FIYATI across such an event, so close(t)/prev_recorded(t) is the
    true one-day total return. Chaining those gives a series whose ratios are correct
    returns over any interval, which is all the pipeline asks of it.

    OHLC fields are scaled by the same running factor so they stay on the series'
    scale rather than silently mixing raw prints with adjusted closes.
    """
    by_ticker: dict[str, list[dict]] = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append(r)

    out: list[dict] = []
    for series in by_ticker.values():
        series.sort(key=lambda r: r["price_date"])
        level = series[0]["close_try"]
        factor = Decimal(1)
        prev_raw_close = series[0]["close_try"]

        for i, r in enumerate(series):
            raw_close = r["close_try"]
            if i > 0:
                prev_recorded = r.pop("_prev_close", None)
                base = (
                    prev_recorded
                    if prev_recorded and prev_recorded > 0
                    else prev_raw_close
                )
                if base > 0:
                    level = level * raw_close / base
                factor = level / raw_close if raw_close > 0 else factor
            else:
                r.pop("_prev_close", None)

            def _scale(v: Decimal | None) -> Decimal | None:
                return (v * factor).quantize(Decimal("0.0001")) if v is not None else None

            prev_raw_close = raw_close
            r["close_try"] = level.quantize(Decimal("0.0001"))
            r["open_try"] = _scale(r["open_try"])
            r["high_try"] = _scale(r["high_try"])
            r["low_try"] = _scale(r["low_try"])
            out.append(r)
    return out


def _read_month(path: Path) -> list[dict]:
    if path.suffix.lower() == ".zip":
        # Read in place - extracting 1.8 GB of archive is not affordable here.
        with zipfile.ZipFile(path) as zf:
            name = next((n for n in zf.namelist() if n.lower().endswith(".csv")), None)
            if name is None:
                return []
            raw = zf.read(name)
    else:
        raw = path.read_bytes()
    return _rows_from_csv(raw.decode("utf-8", errors="replace"))


async def _store(rows: list[dict]) -> int:
    if not rows:
        return 0
    async with get_session() as session:
        for i in range(0, len(rows), 4000):
            chunk = rows[i : i + 4000]
            stmt = pg_insert(PriceHistory.__table__).values(chunk)
            await session.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_price_ticker_date",
                    set_={
                        "close_try": stmt.excluded.close_try,
                        "open_try": stmt.excluded.open_try,
                        "high_try": stmt.excluded.high_try,
                        "low_try": stmt.excluded.low_try,
                        "volume": stmt.excluded.volume,
                    },
                )
            )
    return len(rows)


async def main_async(start: str) -> None:
    await init_db()

    if not ARCHIVE.is_dir():
        raise SystemExit(f"Official price archive not found: {ARCHIVE}")

    files = sorted(
        f
        for f in ARCHIVE.iterdir()
        if f.suffix.lower() in {".zip", ".csv"}
        and (m := f.name.split(".M.")[-1][:6])
        and m.isdigit()
        and m >= start.replace("-", "")
    )
    if not files:
        raise SystemExit(f"No bulletin files at or after {start}")

    click.echo(f"Reading {len(files)} monthly bulletins from {files[0].name} ...")

    # The whole span is read before anything is stored: the total-return chain needs a
    # ticker's series unbroken, and a corporate action does not respect month boundaries.
    raw: list[dict] = []
    for path in files:
        raw.extend(_read_month(path))

    tickers = {r["ticker"] for r in raw}
    click.echo(f"  {len(raw):,} equity rows, {len(tickers)} distinct tickers")

    rows = chain_total_return(raw)
    click.echo("  chained to a corporate-action-adjusted total-return series")

    stored = await _store(rows)
    click.echo(f"Done: {stored:,} price rows stored for {len(tickers)} tickers.")


@click.command()
@click.option("--from", "start", default="2015-01", help="First month, YYYY-MM.")
def main(start: str) -> None:
    configure_logging()
    asyncio.run(main_async(start))


if __name__ == "__main__":
    main()
