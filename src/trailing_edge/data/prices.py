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
    Returns {ticker: rows_upserted}. Delisted / missing tickers are skipped with a warning.
    """
    import pandas as pd

    if not tickers:
        return {}

    yf_tickers = [f"{t}.IS" for t in tickers]

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
        _log.error("yfinance_download_failed", error=str(exc))
        return {}

    if df is None or df.empty:
        _log.warning("yfinance_empty_response")
        return {}

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
            ticker = yf_ticker.replace(".IS", "")
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

                insert_stmt = pg_insert(PriceHistory.__table__).values(values_list)
                upsert_stmt = insert_stmt.on_conflict_do_update(
                    constraint="uq_price_ticker_date",
                    set_={
                        "close_try": insert_stmt.excluded.close_try,
                        "open_try": insert_stmt.excluded.open_try,
                        "high_try": insert_stmt.excluded.high_try,
                        "low_try": insert_stmt.excluded.low_try,
                        "volume": insert_stmt.excluded.volume,
                    },
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


async def get_price_and_date_after_days(
    ticker: str,
    from_date: date,
    horizon_days: int,
    session: AsyncSession | None = None,
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
    """

    async def _query(s: AsyncSession) -> tuple[Decimal | None, date | None]:
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
