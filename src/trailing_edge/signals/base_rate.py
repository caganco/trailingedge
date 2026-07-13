"""Base-rate statistics for insider cluster signals.

What this module enforces, and why
----------------------------------
It previously reported a bare hit rate and mean over whatever rows existed, with
no interval, no significance test, and no minimum sample size. At N=29 that is
not a weak result - it is not a result at all: the 95% interval around a 55% hit
rate spans roughly [37%, 73%], which contains the 50% null with room to spare.
A lone point estimate invites the reader (and the author) to see an edge in
noise.

Three things are now enforced:

1. The PRIMARY metric is the market-adjusted abnormal return, not the raw return.
   See ``signals/abnormal.py``: BIST quotes nominal TRY under high inflation, so
   a raw hit rate looks good for free.
2. Every estimate carries an interval (Wilson score for the hit rate) and a
   two-sided test (t-stat on mean AR against a zero null).
3. An underpowered sample returns ``verdict=INSUFFICIENT_POWER`` and refuses to
   claim anything in either direction. Distinguishing a 55% hit rate from a 50%
   null at alpha=0.05 with 80% power needs ~784 observations:
       n = (z_alpha/2 + z_beta)^2 * p(1-p) / (p1 - p0)^2
   This is a gate, not a warning string - ``verdict`` is what callers must read.

Statistical note: the p-value uses a normal approximation to the t distribution
(via ``math.erfc``), accurate at the sample sizes the power gate admits and
avoiding a scipy dependency for a single CDF. Below the gate the verdict already
refuses to interpret it.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select

from trailing_edge.core.config import get_config
from trailing_edge.core.db import get_session
from trailing_edge.core.logging import get_logger
from trailing_edge.models.signal import InsiderCluster, SignalOutcome

_log = get_logger(__name__)

_ZERO = Decimal("0.00")
_Z_95 = 1.959963985  # two-sided 95%
_Z_POWER_80 = 0.8416212336  # one-sided 80% power

# Verdicts. Downstream reports must branch on these, never on the raw numbers.
INSUFFICIENT_POWER = "INSUFFICIENT_POWER"
NO_EDGE_DETECTED = "NO_EDGE_DETECTED"
EDGE_DETECTED = "EDGE_DETECTED"
NEGATIVE_EDGE = "NEGATIVE_EDGE"
SURVIVORSHIP_BIASED = "SURVIVORSHIP_BIASED"

# Above this share of clusters dropped for want of price data, no verdict is safe.
#
# A cluster with no price series contributes nothing to the mean - and the tickers
# that have no price series are overwhelmingly the ones that were DELISTED. Measured
# on a real run: 326 of 910 clusters (36%) silently vanished this way, and the names
# behind them (ACSEL, ANELT, ARBUL, BISAS, BMEKS...) are exactly the small caps that
# went to zero. Their absence is not random; it removes the worst outcomes and leaves
# the mean looking like an edge. This gate exists because the number it suppresses -
# +2.45% abnormal at 20 days, t=6.07 - was the most convincing wrong answer this
# pipeline has ever produced.
_MAX_ATTRITION = 0.10


@dataclass
class BaseRateStats:
    horizon_days: int
    benchmark_ticker: str | None
    total_signals: int
    signals_with_outcome: int

    # Primary: market-adjusted (abnormal) return, in percent.
    hit_rate_pct: Decimal
    hit_rate_ci_low_pct: Decimal
    hit_rate_ci_high_pct: Decimal
    mean_abnormal_return_pct: Decimal
    median_abnormal_return_pct: Decimal
    t_stat: Decimal
    p_value: Decimal
    best_abnormal_return_pct: Decimal
    worst_abnormal_return_pct: Decimal

    # Secondary, audit only: what the raw number would have claimed, and how much
    # of it was simply the market. Never report these as evidence of an edge.
    mean_raw_return_pct: Decimal
    mean_benchmark_return_pct: Decimal

    # Share of clusters that could not be priced at all. Their tickers are mostly
    # DELISTED, so the attrition is not random - it deletes the worst outcomes.
    attrition_pct: Decimal

    required_n_for_power: int
    verdict: str


def required_n(
    p_null: float = 0.50,
    p_alt: float = 0.55,
    z_alpha: float = _Z_95,
    z_power: float = _Z_POWER_80,
) -> int:
    """Observations needed to distinguish p_alt from p_null (two-sided, 80% power)."""
    effect = abs(p_alt - p_null)
    if effect == 0:
        raise ValueError("p_alt must differ from p_null")
    return math.ceil((z_alpha + z_power) ** 2 * p_null * (1 - p_null) / effect**2)


def wilson_interval(hits: int, n: int, z: float = _Z_95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion, as fractions in [0, 1].

    Preferred over the normal (Wald) interval, which misbehaves at small n and
    near 0/1 - exactly the regime this project keeps landing in.
    """
    if n <= 0:
        return 0.0, 0.0
    p = hits / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2))
    return max(0.0, center - half), min(1.0, center + half)


def t_test_vs_zero(values: list[float]) -> tuple[float, float]:
    """One-sample t-stat and two-sided p-value against a zero-mean null."""
    n = len(values)
    if n < 2:
        return 0.0, 1.0
    sd = statistics.stdev(values)
    if sd == 0:
        return 0.0, 1.0
    t = statistics.fmean(values) / (sd / math.sqrt(n))
    return t, math.erfc(abs(t) / math.sqrt(2))


def _dec(v: float, places: str = "0.01") -> Decimal:
    return Decimal(str(v)).quantize(Decimal(places), rounding=ROUND_HALF_UP)


def _empty(horizon_days: int, total: int, need: int) -> BaseRateStats:
    return BaseRateStats(
        horizon_days=horizon_days,
        benchmark_ticker=None,
        total_signals=total,
        signals_with_outcome=0,
        hit_rate_pct=_ZERO,
        hit_rate_ci_low_pct=_ZERO,
        hit_rate_ci_high_pct=_ZERO,
        mean_abnormal_return_pct=_ZERO,
        median_abnormal_return_pct=_ZERO,
        t_stat=_ZERO,
        p_value=Decimal("1.0000"),
        best_abnormal_return_pct=_ZERO,
        worst_abnormal_return_pct=_ZERO,
        mean_raw_return_pct=_ZERO,
        mean_benchmark_return_pct=_ZERO,
        attrition_pct=_ZERO,
        required_n_for_power=need,
        verdict=INSUFFICIENT_POWER,
    )


async def compute_base_rate(
    horizon_days: int,
    min_cluster_score: float = 0.0,
) -> BaseRateStats:
    """
    Base-rate stats for clusters with score >= min_cluster_score, at one horizon.

    Rows are keyed on ``abnormal_return_pct``: an outcome whose benchmark leg
    could not be priced is NOT counted, because averaging market-adjusted and raw
    returns together would silently corrupt the estimate. ``verdict`` is the field
    to read - below the power gate the point estimates mean nothing on their own.
    """
    cfg = get_config()["signals"]["returns"]
    need = int(cfg.get("min_signals_for_verdict") or required_n())
    min_score = Decimal(str(min_cluster_score))

    async with get_session() as session:
        stmt = (
            select(
                SignalOutcome.abnormal_return_pct,
                SignalOutcome.return_pct,
                SignalOutcome.benchmark_return_pct,
                SignalOutcome.benchmark_ticker,
            )
            .join(InsiderCluster, InsiderCluster.id == SignalOutcome.cluster_id)
            .where(
                SignalOutcome.horizon_days == horizon_days,
                InsiderCluster.cluster_score >= min_score,
            )
        )
        rows = (await session.execute(stmt)).all()

    # Clusters that produced NO priceable outcome at all. Counting only the rows that
    # HAVE an outcome hides them - and they are not missing at random.
    async with get_session() as session:
        total_clusters = (
            await session.execute(
                select(func.count())
                .select_from(InsiderCluster)
                .where(InsiderCluster.cluster_score >= min_score)
            )
        ).scalar_one()

    total_signals = len(rows)
    scored = [r for r in rows if r.abnormal_return_pct is not None]

    if not scored:
        if total_signals:
            _log.warning(
                "base_rate_no_abnormal_returns",
                horizon=horizon_days,
                total_signals=total_signals,
                hint="benchmark prices missing - run `prices backfill`",
            )
        return _empty(horizon_days, total_signals, need)

    ar = [float(r.abnormal_return_pct) for r in scored]
    raw = [float(r.return_pct) for r in scored if r.return_pct is not None]
    bench = [
        float(r.benchmark_return_pct)
        for r in scored
        if r.benchmark_return_pct is not None
    ]

    n = len(ar)
    hits = sum(1 for v in ar if v > 0)
    ci_low, ci_high = wilson_interval(hits, n)
    t_stat, p_value = t_test_vs_zero(ar)
    mean_ar = statistics.fmean(ar)

    attrition = (total_clusters - n) / total_clusters if total_clusters else 0.0

    if attrition > _MAX_ATTRITION:
        # Not a footnote. A third of the clusters vanishing because their companies
        # were delisted removes the worst outcomes and manufactures an edge; no
        # verdict computed on the survivors can be trusted.
        verdict = SURVIVORSHIP_BIASED
    elif n < need:
        verdict = INSUFFICIENT_POWER
    elif p_value >= 0.05:
        verdict = NO_EDGE_DETECTED
    elif mean_ar > 0:
        verdict = EDGE_DETECTED
    else:
        verdict = NEGATIVE_EDGE

    _log.info(
        "base_rate_computed",
        horizon=horizon_days,
        n=n,
        required_n=need,
        mean_abnormal_return_pct=round(mean_ar, 4),
        p_value=round(p_value, 4),
        attrition_pct=round(attrition * 100, 1),
        verdict=verdict,
    )

    return BaseRateStats(
        horizon_days=horizon_days,
        benchmark_ticker=scored[0].benchmark_ticker,
        total_signals=total_signals,
        signals_with_outcome=n,
        hit_rate_pct=_dec(hits / n * 100),
        hit_rate_ci_low_pct=_dec(ci_low * 100),
        hit_rate_ci_high_pct=_dec(ci_high * 100),
        mean_abnormal_return_pct=_dec(mean_ar),
        median_abnormal_return_pct=_dec(statistics.median(ar)),
        t_stat=_dec(t_stat),
        p_value=_dec(p_value, "0.0001"),
        best_abnormal_return_pct=_dec(max(ar)),
        worst_abnormal_return_pct=_dec(min(ar)),
        mean_raw_return_pct=_dec(statistics.fmean(raw)) if raw else _ZERO,
        mean_benchmark_return_pct=_dec(statistics.fmean(bench)) if bench else _ZERO,
        attrition_pct=_dec(attrition * 100),
        required_n_for_power=need,
        verdict=verdict,
    )
