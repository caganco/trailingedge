"""yfinance must not overwrite an exchange-bulletin price row.

Two writers touch price_history and they do NOT mean the same thing by close_try:

  - scripts/load_official_prices.py stores a chained corporate-action-adjusted
    total-return index, with the matching raw print in raw_close_try.
  - data/prices.py (yfinance) stores yfinance's own adjusted close, and knows nothing
    about raw_close_try.

`trailing-edge prices backfill` pulls every KAP insider ticker from yfinance, so without
a guard it would walk over the entire bulletin-derived series - replacing a
survivorship-clean chain (yfinance serves NOTHING for a delisted BIST ticker) with a
biased one, dropping the VBTS flags, and leaving close_try and raw_close_try sourced from
two different providers while the cost model reads both.

This pins the guard. It is the kind of regression that produces no error, only a quietly
worse number.
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from trailing_edge.models.signal import PriceHistory

_TICKER = "ZZTEST"
_DAY = date(2019, 3, 14)


async def _upsert_like_yfinance(session, close: Decimal) -> None:
    """Exactly the statement data/prices.py issues, guard included."""
    stmt = pg_insert(PriceHistory.__table__).values(
        [{"ticker": _TICKER, "price_date": _DAY, "close_try": close, "volume": 1}]
    )
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
            where=PriceHistory.__table__.c.raw_close_try.is_(None),
        )
    )


@pytest.mark.asyncio
async def test_yfinance_cannot_overwrite_a_bulletin_row(db_session):
    await db_session.execute(delete(PriceHistory).where(PriceHistory.ticker == _TICKER))

    # a bulletin row: chained index 100, the stock actually printed 2.50
    await db_session.execute(
        pg_insert(PriceHistory.__table__).values(
            [
                {
                    "ticker": _TICKER,
                    "price_date": _DAY,
                    "close_try": Decimal("100.0000"),
                    "raw_close_try": Decimal("2.5000"),
                    "volume": 1,
                }
            ]
        )
    )

    await _upsert_like_yfinance(db_session, Decimal("2.5000"))

    row = (
        await db_session.execute(
            select(PriceHistory.close_try, PriceHistory.raw_close_try).where(
                PriceHistory.ticker == _TICKER
            )
        )
    ).one()

    assert row.close_try == Decimal("100.0000"), "the bulletin's chained index must survive"
    assert row.raw_close_try == Decimal("2.5000")

    await db_session.execute(delete(PriceHistory).where(PriceHistory.ticker == _TICKER))


@pytest.mark.asyncio
async def test_yfinance_still_fills_a_row_the_bulletin_never_covered(db_session):
    """The guard must not become a blanket refusal: XU100 is an index, appears in no
    equity bulletin, and yfinance is the only source for it. A row with no raw_close_try
    did not come from the bulletin and is still yfinance's to write."""
    await db_session.execute(delete(PriceHistory).where(PriceHistory.ticker == _TICKER))

    await _upsert_like_yfinance(db_session, Decimal("11.0000"))
    await _upsert_like_yfinance(db_session, Decimal("12.0000"))  # a later correction

    row = (
        await db_session.execute(
            select(PriceHistory.close_try).where(PriceHistory.ticker == _TICKER)
        )
    ).one()

    assert row.close_try == Decimal("12.0000")

    await db_session.execute(delete(PriceHistory).where(PriceHistory.ticker == _TICKER))
