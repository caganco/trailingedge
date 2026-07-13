"""Does the opportunistic subset clear the spread?

The pre-registered test. Definition, horizon, minimum N and decision rule are frozen in
docs/stage0/OPPORTUNISTIC_CLASSIFIER.md, committed before this was run.

Primary: mean abnormal return NET of the per-trade round-trip cost, at 20 trading days,
on opportunistic clusters. Tradeable only if positive with p < 0.05 on N >= 200.

Usage:
    uv run python scripts/opportunistic_test.py
"""
from __future__ import annotations

import asyncio
import math
import statistics
import sys
from collections import Counter, defaultdict
from decimal import Decimal

import click

sys.path.insert(0, "src")

from sqlalchemy import text  # noqa: E402

from trailing_edge.core.db import get_session, init_db  # noqa: E402
from trailing_edge.core.logging import configure_logging  # noqa: E402
from trailing_edge.signals.costs import round_trip_cost  # noqa: E402
from trailing_edge.signals.opportunistic import (  # noqa: E402
    classify_cluster,
    classify_insider,
)

PRIMARY_HORIZON = 20  # frozen
MIN_N = 200  # frozen
ORDER_TRY = Decimal("25000")
_LOOKBACK = 30


async def main_async() -> None:
    await init_db()

    async with get_session() as session:
        # Every insider purchase, with the date its disclosure became PUBLIC. The
        # classifier may only see what was visible at the signal date.
        hist_rows = (
            await session.execute(
                text(
                    """
                    SELECT t.insider_name, t.transaction_date, d.published_at::date AS pub
                    FROM kap_insider_transactions t
                    JOIN kap_disclosures d ON d.id = t.disclosure_id
                    WHERE t.transaction_type = 'BUY' AND d.published_at IS NOT NULL
                    """
                )
            )
        ).all()

        history: dict[str, list[tuple]] = defaultdict(list)
        for name, tx_date, pub in hist_rows:
            history[name].append((pub, tx_date))

        rows = (
            await session.execute(
                text(
                    """
                    SELECT o.cluster_id, o.horizon_days, o.abnormal_return_pct,
                           o.entry_date, c.ticker, c.unique_insiders, c.window_end
                    FROM signal_outcomes o
                    JOIN insider_clusters c ON c.id = o.cluster_id
                    WHERE o.abnormal_return_pct IS NOT NULL AND o.entry_date IS NOT NULL
                    """
                )
            )
        ).all()

        cache: dict[tuple, tuple | None] = {}

        async def cost_for(ticker: str, entry) -> Decimal | None:
            key = (ticker, entry)
            if key not in cache:
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
                if len(px) < 22 or any(r[4] is None for r in px):
                    cache[key] = None
                else:
                    px = list(reversed(px))
                    closes = [r[0] for r in px]
                    highs = [r[1] or r[0] for r in px]
                    lows = [r[2] or r[0] for r in px]
                    # ADV and the tick floor take the traded price, not the adjusted index
                    # - see scripts/net_of_cost.py and migration 0008.
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
                        closes, highs, lows, ORDER_TRY, adv, last_traded_price=raws[-1]
                    )
                    cache[key] = (rt.total_pct,) if rt else None
            got = cache[key]
            return got[0] if got else None

        buckets: dict[tuple[str, int], list[tuple[float, float]]] = defaultdict(list)
        cls_counts: Counter = Counter()

        for cid, horizon, ar, entry, ticker, insiders, window_end in rows:
            # Classify each insider using ONLY filings public at the cluster's own date.
            classes = []
            for name in insiders or []:
                prior = [
                    tx for pub, tx in history.get(name, [])
                    if pub <= window_end and tx < window_end
                ]
                classes.append(classify_insider(prior, window_end))
            klass = classify_cluster(classes)

            if horizon == PRIMARY_HORIZON:
                cls_counts[klass.value] += 1

            cost = await cost_for(ticker, entry)
            if cost is None:
                continue
            buckets[(klass.value, horizon)].append((float(ar), float(cost)))

    click.echo("")
    click.echo("=== Pre-registered test: does the opportunistic subset clear the spread? ===")
    click.echo("    definition frozen in docs/stage0/OPPORTUNISTIC_CLASSIFIER.md")
    click.echo(f"    primary horizon {PRIMARY_HORIZON}d, min N {MIN_N}, order {ORDER_TRY:,.0f} TRY")
    click.echo("")
    click.echo(f"    cluster classes at {PRIMARY_HORIZON}d: " + ", ".join(
        f"{k}={v}" for k, v in sorted(cls_counts.items())
    ))
    click.echo("")

    hdr = f"{'CLASS':>14} {'HZN':>4} {'N':>5} {'GROSS%':>8} {'COST%':>7} {'NET%':>7} {'t':>7}  VERDICT"
    click.echo(hdr)
    click.echo("-" * len(hdr))

    for klass in ("OPPORTUNISTIC", "ROUTINE", "UNCLASSIFIED"):
        for horizon in (5, 20, 60):
            pairs = buckets.get((klass, horizon), [])
            if not pairs:
                continue
            net = [a - c for a, c in pairs]
            n = len(net)
            mean_net = statistics.fmean(net)
            sd = statistics.stdev(net) if n > 1 else 0.0
            t = mean_net / (sd / math.sqrt(n)) if sd > 0 else 0.0

            primary = horizon == PRIMARY_HORIZON
            if not primary:
                verdict = "(secondary)"
            elif n < MIN_N:
                verdict = f"INSUFFICIENT_POWER (n<{MIN_N})"
            elif mean_net > 0 and abs(t) > 1.96:
                verdict = "TRADEABLE"
            elif abs(t) <= 1.96:
                verdict = "NO EDGE (net)"
            else:
                verdict = "LOSES MONEY (net)"

            mark = " *" if primary else "  "
            click.echo(
                f"{klass:>14} {horizon:>3}d{mark}{n:>4} "
                f"{statistics.fmean([a for a, _ in pairs]):>8.2f} "
                f"{statistics.fmean([c for _, c in pairs]):>7.2f} "
                f"{mean_net:>7.2f} {t:>7.2f}  {verdict}"
            )
    click.echo("")
    click.echo("  * = the pre-registered primary test. Everything else is secondary.")
    click.echo("")


@click.command()
def main() -> None:
    configure_logging()
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
