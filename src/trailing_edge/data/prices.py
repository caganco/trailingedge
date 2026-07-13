"""BIST günlük fiyat verisi - yfinance TICKER.IS formatı."""
from __future__ import annotations

import asyncio
import functools
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from trailing_edge.core.db import get_session
from trailing_edge.core.logging import get_logger
from trailing_edge.models.signal import PriceHistory

_log = get_logger(__name__)


# BIST tickers that were RENAMED, not delisted. KAP files them under the old code;
# yfinance only serves the new one, so without this map their price history looks
# missing - and a cluster with no prices is silently dropped from the base rate,
# which reads exactly like survivorship bias. Verified individually against yfinance
# (each new symbol returns a full 2015-2017 series; each old one returns nothing).
_TICKER_ALIASES: dict[str, str] = {
    "GYHOL": "GLYHO",  # Global Yatirim Holding
    "AKFEN": "AKFGY",  # Akfen -> Akfen Gayrimenkul
}


def _yf_symbol(ticker: str) -> str:
    return f"{_TICKER_ALIASES.get(ticker, ticker)}.IS"


def _sync_yf_download(yf_tickers: list[str], start: str, end: str):
    import yfinance as yf

    return yf.download(
        yf_tickers,
        start=start,
        end=end,
        auto_adjust=True,
        progress=False,
        threads=True,
    )


async def fetch_and_store_prices(
    tickers: list[str],
    start_date: date,
    end_date: date,
) -> dict[str, int]:
    """
    Fetch OHLCV for each ticker from yfinance and upsert into price_history.

    tickers: BIST short codes (e.g. ["ASELS", "ISCTR"]) - ".IS" suffix added here.
    Returns {ticker: rows_upserted}. Tickers yfinance genuinely has no data for are
    reported as 0 rows.

    Downloaded in SMALL BATCHES, and every batch that comes back short is retried one
    ticker at a time.

    One request for all 245 tickers looked efficient and quietly lost a third of them:
    yfinance answers a batch containing an unknown symbol with an error naming several
    ("Quote not found for symbol: GEREL, VRSGS.IS") and drops the innocent ones with it.
    ACSEL, ANELT and ATLAS each have 750+ days of history and still ended up with no
    price rows at all. Because a cluster with no prices is silently excluded from the
    base rate, and the tickers most likely to be dropped are the obscure ones, the loss
    read exactly like survivorship bias - it was a batching bug wearing its costume.
    """
    if not tickers:
        return {}

    results: dict[str, int] = {}
    batch_size = 20

    for i in range(0, len(tickers), batch_size):
        batch = tickers[i : i + batch_size]
        got = await _fetch_batch(batch, start_date, end_date)

        # Any ticker the batch did not deliver is retried alone, so one bad symbol
        # cannot take its neighbours down with it.
        missing = [t for t in batch if got.get(t, 0) == 0]
        for ticker in missing:
            solo = await _fetch_batch([ticker], start_date, end_date)
            got[ticker] = solo.get(ticker, 0)
            if got[ticker] == 0:
                _log.warning("price_ticker_no_data", ticker=ticker)

        results.update(got)

    recovered = sum(1 for v in results.values() if v > 0)
    _log.info(
        "prices_fetch_done",
        requested=len(tickers),
        with_data=recovered,
        without_data=len(tickers) - recovered,
    )
    return results


async def _fetch_batch(
    tickers: list[str],
    start_date: date,
    end_date: date,
) -> dict[str, int]:
    """Download and store one batch. Returns {ticker: rows_stored}, 0 where absent."""
    import pandas as pd

    yf_tickers = [_yf_symbol(t) for t in tickers]
    # Map the downloaded symbol back to the ticker KAP files under.
    by_symbol = {_yf_symbol(t): t for t in tickers}

    loop = asyncio.get_event_loop()
    try:
        df = await loop.run_in_executor(
            None,
            functools.partial(
                _sync_yf_download,
                yf_tickers,
                str(start_date),
                str(end_date + timedelta(days=1)),  # yfinance end is exclusive
            ),
        )
    except Exception as exc:
        _log.warning("yfinance_batch_failed", tickers=len(tickers), error=str(exc))
        return dict.fromkeys(tickers, 0)

    if df is None or df.empty:
        return dict.fromkeys(tickers, 0)

    results: dict[str, int] = {}
    is_multi = isinstance(df.columns, pd.MultiIndex)

    # Determine which MultiIndex level holds ticker names
    ticker_level: int = 1
    if is_multi:
        for lvl in range(df.columns.nlevels):
            vals = df.columns.get_level_values(lvl)
            if any(str(v).endswith(".IS") for v in vals):
                ticker_level = lvl
                break

    async with get_session() as session:
        for yf_ticker in yf_tickers:
            ticker = by_symbol[yf_ticker]
            try:
                if is_multi:
                    level_vals = df.columns.get_level_values(ticker_level)
                    if yf_ticker not in level_vals:
                        _log.warning("price_ticker_not_found", ticker=yf_ticker)
                        results[ticker] = 0
                        continue
                    sub = df.xs(yf_ticker, axis=1, level=ticker_level)
                else:
                    sub = df

                if sub.empty:
                    results[ticker] = 0
                    continue

                # Normalize column names to lowercase
                sub = sub.copy()
                sub.columns = [str(c).lower() for c in sub.columns]

                values_list = []
                for dt_idx, row in sub.iterrows():
                    close_val = row.get("close")
                    if close_val is None or (hasattr(close_val, "__float__") and pd.isna(close_val)):
                        continue
                    price_date = dt_idx.date() if hasattr(dt_idx, "date") else dt_idx

                    def _to_dec(v) -> Decimal | None:
                        if v is None or pd.isna(v):
                            return None
                        return Decimal(str(float(v)))

                    values_list.append(
                        {
                            "ticker": ticker,
                            "price_date": price_date,
                            "open_try": _to_dec(row.get("open")),
                            "high_try": _to_dec(row.get("high")),
                            "low_try": _to_dec(row.get("low")),
                            "close_try": Decimal(str(float(close_val))),
                            "volume": int(row["volume"]) if pd.notna(row.get("volume")) else None,
                        }
                    )

                if not values_list:
                    results[ticker] = 0
                    continue

                insert_stmt = pg_insert(PriceHistory).values(values_list)
                # The exchange bulletin is authoritative and yfinance must not overwrite
                # it. They are not interchangeable sources of the same number:
                #
                #   - the bulletin is survivorship-clean; yfinance serves NOTHING for a
                #     delisted BIST ticker, not even the years it traded.
                #   - the bulletin carries the VBTS flags (gross settlement, suspension).
                #   - scripts/load_official_prices.py stores close_try as a chained
                #     total-return index with the matching raw print in raw_close_try.
                #     Letting yfinance replace close_try alone would leave the pair
                #     inconsistent - an index from one source, a price from another - and
                #     the cost model reads both.
                #
                # So a row that already has raw_close_try came from the bulletin and is
                # left alone. yfinance stays what it is genuinely needed for: the XU100
                # benchmark, which is an index and appears in no equity bulletin.
                upsert_stmt = insert_stmt.on_conflict_do_update(
                    constraint="uq_price_ticker_date",
                    set_={
                        "close_try": insert_stmt.excluded.close_try,
                        "open_try": insert_stmt.excluded.open_try,
                        "high_try": insert_stmt.excluded.high_try,
                        "low_try": insert_stmt.excluded.low_try,
                        "volume": insert_stmt.excluded.volume,
                    },
                    where=PriceHistory.raw_close_try.is_(None),
                )
                await session.execute(upsert_stmt)
                results[ticker] = len(values_list)
                _log.info("prices_stored", ticker=ticker, rows=len(values_list))

            except Exception as exc:
                _log.warning("price_ticker_error", ticker=yf_ticker, error=str(exc))
                results[ticker] = 0

    return results


async def get_price_on_date(
    ticker: str,
    target_date: date,
    session: AsyncSession | None = None,
) -> Decimal | None:
    """Return the close price on or before target_date (nearest trading day)."""

    async def _query(s: AsyncSession) -> Decimal | None:
        result = await s.execute(
            select(PriceHistory.close_try)
            .where(
                PriceHistory.ticker == ticker,
                PriceHistory.price_date <= target_date,
            )
            .order_by(PriceHistory.price_date.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        return Decimal(str(row)) if row is not None else None

    if session is not None:
        return await _query(session)
    async with get_session() as s:
        return await _query(s)


async def get_price_after_days(
    ticker: str,
    from_date: date,
    horizon_days: int,
    session: AsyncSession | None = None,
) -> Decimal | None:
    """
    Return the close price exactly horizon_days trading days after from_date.
    Uses price_history rows (yfinance already excludes weekends/holidays).
    OFFSET horizon_days - 1: e.g. horizon=5 → OFFSET 4 → 5th row after from_date.
    """
    price, _ = await get_price_and_date_after_days(
        ticker, from_date, horizon_days, session=session
    )
    return price


# A trading day the pipeline treats as "the next session" cannot be an arbitrary
# distance away. Beyond this many calendar days from the reference date, the row is not
# the next session at all - it is the first session after a hole in the data.
_MAX_SESSION_GAP_DAYS = 10


async def get_price_and_date_after_days(
    ticker: str,
    from_date: date,
    horizon_days: int,
    session: AsyncSession | None = None,
    max_gap_days: int = _MAX_SESSION_GAP_DAYS,
) -> tuple[Decimal | None, date | None]:
    """
    Close price AND its trading date, horizon_days trading days after from_date.

    The date matters for market adjustment: the benchmark return must span the
    same *calendar* interval the position was actually held, not the same
    trading-day offsets on the benchmark's own calendar. The two calendars
    diverge whenever the stock is halted or suspended (e.g. a BIST single-price
    / VBTS measure), which is exactly the population insider clusters concentrate
    in - so taking offsets on each series independently would silently compare
    mismatched windows.

    Entry (horizon_days=1) additionally REFUSES a row that is not actually adjacent.
    The query asks for the first row after from_date, and if the price history simply
    has no rows near from_date it happily returns one from months later - which the
    caller then books as a t+1 entry. Measured: 1,278 outcomes were entered on
    2015-12-01 because that is where the price series began, for signals fired months
    earlier. Those are not late entries, they are fabricated ones: nobody could have
    bought at a price that had not printed yet. A gap wider than max_gap_days yields
    None, and the cluster is dropped as unpriceable - which is what it is.
    """

    async def _query(s: AsyncSession) -> tuple[Decimal | None, date | None]:
        # The FIRST session after from_date anchors the window. If the series has no
        # row anywhere near from_date, that first row is not "tomorrow" - it is the
        # far side of a hole, and nothing may be entered against it.
        first = (
            await s.execute(
                select(PriceHistory.price_date)
                .where(
                    PriceHistory.ticker == ticker,
                    PriceHistory.price_date > from_date,
                )
                .order_by(PriceHistory.price_date.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if first is None or (first - from_date).days > max_gap_days:
            return None, None

        result = await s.execute(
            select(PriceHistory.close_try, PriceHistory.price_date)
            .where(
                PriceHistory.ticker == ticker,
                PriceHistory.price_date > from_date,
            )
            .order_by(PriceHistory.price_date.asc())
            .offset(horizon_days - 1)
            .limit(1)
        )
        row = result.one_or_none()
        if row is None:
            return None, None
        return Decimal(str(row[0])), row[1]

    if session is not None:
        return await _query(session)
    async with get_session() as s:
        return await _query(s)
