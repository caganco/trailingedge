"""Regime split of the insider-cluster signal: gross and net abnormal return by era.

The question the full 2015-2026 backfill was collected to answer. Every priceable cluster is
bucketed by the era of its own date and reported gross/net at 5/20/60 days, so the pooled
headline can be seen for what it is - an average of a real 2015-2018 edge and a decayed
2021-2026 one.

    uv run python scripts/regime_split.py
"""
from __future__ import annotations

import asyncio
import math
import statistics
import sys
from decimal import Decimal

sys.path.insert(0, "src")

from sqlalchemy import text  # noqa: E402

from trailing_edge.core.db import get_session, init_db  # noqa: E402
from trailing_edge.signals.costs import round_trip_cost  # noqa: E402

ORDER = Decimal("25000")
LOOK = 30


def regime(year: int) -> str:
    if year <= 2018:
        return "2015-2018"
    if year <= 2020:
        return "2019-2020"
    return "2021-2026"


def _t(values: list[float]) -> tuple[float, float]:
    mean = statistics.fmean(values)
    sd = statistics.stdev(values) if len(values) > 1 else 0.0
    t = mean / (sd / math.sqrt(len(values))) if sd > 0 else 0.0
    return mean, t


async def main_async() -> None:
    await init_db()
    async with get_session() as s:
        rows = (
            await s.execute(
                text(
                    """
                    SELECT o.horizon_days, o.abnormal_return_pct, o.entry_date, c.ticker,
                           c.window_end
                    FROM signal_outcomes o
                    JOIN insider_clusters c ON c.id = o.cluster_id
                    WHERE o.abnormal_return_pct IS NOT NULL AND o.entry_date IS NOT NULL
                    """
                )
            )
        ).all()

        cache: dict[tuple, float | None] = {}

        async def cost(ticker: str, entry) -> float | None:
            key = (ticker, entry)
            if key not in cache:
                px = (
                    await s.execute(
                        text(
                            """
                            SELECT close_try, high_try, low_try, volume, raw_close_try
                            FROM price_history
                            WHERE ticker = :t AND price_date < :d
                            ORDER BY price_date DESC LIMIT :n
                            """
                        ),
                        {"t": ticker, "d": entry, "n": LOOK},
                    )
                ).all()
                if len(px) < 22 or any(r[4] is None for r in px):
                    cache[key] = None
                else:
                    px = list(reversed(px))
                    closes = [r[0] for r in px]
                    highs = [r[1] or r[0] for r in px]
                    lows = [r[2] or r[0] for r in px]
                    raws = [r[4] for r in px]
                    adv = Decimal(
                        str(
                            statistics.fmean(
                                float(rc) * float(r[3] or 0)
                                for rc, r in zip(raws, px, strict=True)
                            )
                        )
                    )
                    rt = round_trip_cost(
                        closes, highs, lows, ORDER, adv, last_traded_price=raws[-1]
                    )
                    cache[key] = float(rt.total_pct) if rt else None
            return cache[key]

        buckets: dict[tuple[str, int], list[tuple[float, float]]] = {}
        for hz, ar, entry, ticker, window_end in rows:
            c = await cost(ticker, entry)
            if c is None:
                continue
            buckets.setdefault((regime(window_end.year), hz), []).append(
                (float(ar), float(ar) - c)
            )

    hdr = f"{'REGIME':<12}{'HZN':>4}{'N':>6}{'GROSS%':>8}{'COST%':>9}{'NET%':>8}{'t_net':>7}{'t_gross':>8}"
    print(hdr)
    for reg in ("2015-2018", "2019-2020", "2021-2026"):
        for hz in (5, 20, 60):
            grp = buckets.get((reg, hz), [])
            if len(grp) < 20:
                continue
            gross = [x[0] for x in grp]
            net = [x[1] for x in grp]
            costs = [x[0] - x[1] for x in grp]
            mg, tg = _t(gross)
            mn, tn = _t(net)
            print(
                f"{reg:<12}{hz:>3}d{len(grp):>6}{mg:>8.2f}{statistics.fmean(costs):>9.2f}"
                f"{mn:>8.2f}{tn:>7.2f}{tg:>8.2f}"
            )
        print()


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
