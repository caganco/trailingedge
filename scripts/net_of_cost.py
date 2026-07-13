"""Does the abnormal return survive the cost of trading it?

The base rate says +1.46% at 5 days and +2.04% at 20 days, before costs. This is the
test that matters, and it is a hard one here: insider clusters fire in illiquid BIST
small caps - the names with the widest spreads. A flat fee would flatter the answer, so
the spread is estimated per trade from that stock's own OHLC (Abdi-Ranaldo 2017), which
is also the only estimator that works on the delisted names the exchange bulletin gives
us and no quote feed does.

Clusters whose spread cannot be estimated are DROPPED, not priced at zero: a trade whose
cost is unknown is not a trade with no cost.

Usage:
    uv run python scripts/net_of_cost.py
    uv run python scripts/net_of_cost.py --order-try 50000
"""
from __future__ import annotations

import asyncio
import math
import statistics
import sys
from decimal import Decimal

import click

sys.path.insert(0, "src")

from sqlalchemy import text  # noqa: E402

from trailing_edge.core.db import get_session, init_db  # noqa: E402
from trailing_edge.core.logging import configure_logging  # noqa: E402
from trailing_edge.signals.base_rate import wilson_interval  # noqa: E402
from trailing_edge.signals.costs import round_trip_cost  # noqa: E402

_LOOKBACK = 30  # trading days of OHLC before entry, for the spread estimator


async def main_async(order_try: Decimal) -> None:
    await init_db()

    async with get_session() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT o.horizon_days, o.abnormal_return_pct, o.entry_date, c.ticker
                    FROM signal_outcomes o
                    JOIN insider_clusters c ON c.id = o.cluster_id
                    WHERE o.abnormal_return_pct IS NOT NULL AND o.entry_date IS NOT NULL
                    """
                )
            )
        ).all()

        # One OHLC pull per (ticker, entry_date): the estimator needs the 30 sessions
        # before entry, which is information available at entry - no look-ahead.
        cache: dict[tuple[str, object], tuple | None] = {}

        async def window(ticker: str, entry) -> tuple | None:
            key = (ticker, entry)
            if key in cache:
                return cache[key]
            px = (
                await session.execute(
                    text(
                        """
                        SELECT close_try, high_try, low_try, volume, raw_close_try
                        FROM price_history
                        WHERE ticker = :t AND price_date < :d
                        ORDER BY price_date DESC LIMIT :n
                        """
                    ),
                    {"t": ticker, "d": entry, "n": _LOOKBACK},
                )
            ).all()
            if len(px) < 22:
                cache[key] = None
                return None
            px = list(reversed(px))
            closes = [r[0] for r in px]
            highs = [r[1] or r[0] for r in px]
            lows = [r[2] or r[0] for r in px]
            vols = [r[3] or 0 for r in px]

            # close_try is the corporate-action-adjusted INDEX, which is what the spread
            # estimator wants (it is scale-free and reads the adjusted series). The tick
            # floor and ADV are properties of the traded PRICE, and BIST bonus issues push
            # the two apart - median 0.98x but up to 118x. Using the index here understated
            # the tick floor and inflated ADV, and both errors land in the cost model.
            raws = [r[4] for r in px]
            if any(r is None for r in raws):
                cache[key] = None  # pre-0008 row: refuse rather than fall back to the index
                return None
            adv = Decimal(
                str(statistics.fmean(float(c) * float(v) for c, v in zip(raws, vols, strict=True)))
            )
            cache[key] = (closes, highs, lows, adv, raws[-1])
            return cache[key]

        by_h: dict[int, list[tuple[float, float]]] = {}
        dropped = 0
        costs: list[float] = []

        for horizon, ar, entry, ticker in rows:
            w = await window(ticker, entry)
            if w is None:
                dropped += 1
                continue
            closes, highs, lows, adv, last_price = w
            rt = round_trip_cost(
                closes, highs, lows, order_try, adv, last_traded_price=last_price
            )
            if rt is None:
                dropped += 1
                continue
            costs.append(float(rt.total_pct))
            by_h.setdefault(horizon, []).append((float(ar), float(rt.total_pct)))

    if not by_h:
        click.echo("No priceable clusters.")
        return

    click.echo("")
    click.echo(f"=== Abnormal return, NET of round-trip cost (order {order_try:,.0f} TRY) ===")
    click.echo("    spread: Abdi-Ranaldo (2017) from the stock's own OHLC, per trade")
    click.echo(f"    dropped (no cost estimate): {dropped}")
    if costs:
        cs = sorted(costs)
        click.echo(
            f"    round-trip cost: median {cs[len(cs)//2]:.2f}%  "
            f"p25 {cs[len(cs)//4]:.2f}%  p75 {cs[3*len(cs)//4]:.2f}%"
        )
    click.echo("")
    hdr = f"{'HORIZON':>7} {'N':>5} {'GROSS AR%':>10} {'COST%':>7} {'NET AR%':>8} {'HIT%':>6} {'95% CI':>15} {'t':>6}  VERDICT"
    click.echo(hdr)
    click.echo("-" * len(hdr))

    for horizon in sorted(by_h):
        pairs = by_h[horizon]
        gross = [a for a, _ in pairs]
        net = [a - c for a, c in pairs]
        n = len(net)
        mean_net = statistics.fmean(net)
        sd = statistics.stdev(net) if n > 1 else 0.0
        t = mean_net / (sd / math.sqrt(n)) if sd > 0 else 0.0
        hits = sum(1 for v in net if v > 0)
        lo, hi = wilson_interval(hits, n)

        if abs(t) < 1.96:
            verdict = "NO EDGE (net)"
        elif mean_net > 0:
            verdict = "EDGE SURVIVES COSTS"
        else:
            verdict = "LOSES MONEY (net)"

        click.echo(
            f"{horizon:>6}d {n:>5} {statistics.fmean(gross):>10.2f} "
            f"{statistics.fmean([c for _, c in pairs]):>7.2f} {mean_net:>8.2f} "
            f"{hits/n*100:>6.1f} {f'[{lo*100:.1f}, {hi*100:.1f}]':>15} {t:>6.2f}  {verdict}"
        )
    click.echo("")


@click.command()
@click.option(
    "--order-try",
    default=25000.0,
    help="Position size in TRY. Retail default; impact scales with sqrt(size/ADV).",
)
def main(order_try: float) -> None:
    configure_logging()
    asyncio.run(main_async(Decimal(str(order_try))))


if __name__ == "__main__":
    main()
