"""Report forward-return base rates for detected insider clusters.

This script used to compute its own returns, and did so incorrectly. It is worth
recording exactly how, because the failure is the standard one:

  - It entered at ``window_start`` - the date of the FIRST insider transaction in
    the cluster. That date is private until the KAP filing appears, so the entry
    price was one nobody could have traded at. Under SPK II-15.1 Art. 11 an insider
    owes no disclosure at all until their transactions cross a cumulative 250,000 TRY
    threshold within the calendar year, so the gap between trading and filing is not
    a day or two - it can run to months. The script booked every day of that gap as
    free return.
  - It compared the result against "random = ~50% up, ~0% mean". That prior is wrong
    for BIST: returns are quoted in nominal TRY under high inflation, so a randomly
    chosen stock rises well over half the time and its mean return is not zero.
    Market drift was being reported as signal.

Both problems were already solved elsewhere in this codebase; the script simply did
not use the solution. ``signals/entry_timing.py`` derives a look-ahead-safe signal
date from the disclosures' ``published_at`` (correction-aware) and enters at t+1.
``signals/returns.py`` market-adjusts every outcome against XU100 over the position's
own holding interval. ``signals/base_rate.py`` attaches a Wilson interval, a t-test,
and a power gate.

So this script no longer computes anything. It reads what that pipeline produced and
prints it. To change the numbers below, change the pipeline - not this file.

Usage:
    uv run python scripts/check_forward_returns.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from trailing_edge.core.config import get_config  # noqa: E402
from trailing_edge.core.db import init_db  # noqa: E402
from trailing_edge.signals.base_rate import (  # noqa: E402
    EDGE_DETECTED,
    INSUFFICIENT_POWER,
    NEGATIVE_EDGE,
    compute_base_rate,
)

_VERDICT_NOTE = {
    INSUFFICIENT_POWER: (
        "Sample too small to separate any edge from chance. The estimates above are "
        "NOT evidence in either direction - do not read a hit rate off them."
    ),
    NEGATIVE_EDGE: (
        "Mean abnormal return is significantly BELOW zero. After costs this signal "
        "loses money."
    ),
    EDGE_DETECTED: (
        "Mean abnormal return is significantly above zero BEFORE costs. Spread and "
        "market impact are not deducted here, and on the illiquid names insider "
        "clusters favour that gap is not small. This is not yet a green light."
    ),
}
_DEFAULT_NOTE = (
    "No abnormal return distinguishable from zero at the 5% level. That is a null "
    "result, and it is the honest one to publish."
)


async def main() -> None:
    await init_db()
    cfg = get_config()["signals"]["returns"]
    horizons: list[int] = cfg["horizons"]

    print()
    print("=== Insider-cluster forward returns (market-adjusted) ===")
    print(f"Benchmark: {cfg['benchmark']}   Entry: t+1 after public KAP disclosure")
    print()

    header = (
        f"{'HORIZON':>7} {'N':>5} {'HIT%':>6} {'95% CI':>16} "
        f"{'MEAN AR%':>9} {'MED AR%':>8} {'t':>6} {'p':>8}  VERDICT"
    )
    print(header)
    print("-" * len(header))

    computed = []
    for horizon in horizons:
        s = await compute_base_rate(horizon)
        computed.append(s)

        if s.signals_with_outcome == 0:
            print(f"{horizon:>6}d {0:>5} {'-':>6} {'-':>16} {'-':>9} {'-':>8} {'-':>6} {'-':>8}  NO DATA")
            continue

        ci = f"[{s.hit_rate_ci_low_pct:.1f}, {s.hit_rate_ci_high_pct:.1f}]"
        print(
            f"{horizon:>6}d {s.signals_with_outcome:>5} {s.hit_rate_pct:>6.1f} {ci:>16} "
            f"{s.mean_abnormal_return_pct:>9.2f} {s.median_abnormal_return_pct:>8.2f} "
            f"{s.t_stat:>6.2f} {s.p_value:>8.4f}  {s.verdict}"
        )

    print()
    print("AR = abnormal return = stock return - benchmark return over the same held")
    print("interval. The raw return is retained in the database for audit but is not")
    print("evidence: it credits BIST's nominal drift and the stock's beta to the signal.")
    print()

    for s in computed:
        if s.signals_with_outcome == 0:
            continue
        print(f"  {s.horizon_days}d [{s.verdict}] {_VERDICT_NOTE.get(s.verdict, _DEFAULT_NOTE)}")
        if s.verdict == INSUFFICIENT_POWER:
            print(
                f"       n={s.signals_with_outcome}; "
                f"~{s.required_n_for_power} needed to detect a 55% hit rate at 80% power."
            )
        print(
            f"       raw return would have claimed {s.mean_raw_return_pct:+.2f}%, "
            f"of which the market alone gave {s.mean_benchmark_return_pct:+.2f}%."
        )
    print()


if __name__ == "__main__":
    asyncio.run(main())
