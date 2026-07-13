"""Ranked daily insider cluster signal report - stdout table + JSON file."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import click

from trailing_edge.core.config import get_config
from trailing_edge.core.logging import get_logger
from trailing_edge.models.signal import InsiderCluster
from trailing_edge.signals.base_rate import BaseRateStats, compute_base_rate
from trailing_edge.signals.cluster import detect_clusters
from trailing_edge.signals.returns import calculate_outcomes

_log = get_logger(__name__)


@dataclass
class DailyReport:
    as_of_date: date
    clusters: list[InsiderCluster]
    base_rates: dict[int, BaseRateStats]  # key: horizon_days
    report_path: Path


def _fmt_pct(v: Decimal | None) -> str:
    return f"{v:.1f}%" if v is not None else "n/a"


def _print_table(as_of_date: date, clusters: list[InsiderCluster], base_rates: dict[int, BaseRateStats]) -> None:
    horizons = sorted(base_rates)
    header_hr = "  ".join(f"{h}d:{_fmt_pct(base_rates[h].hit_rate_pct)}" for h in horizons)

    col_w = {
        "ticker": max(6, max((len(c.ticker) for c in clusters), default=6)),
        "score": 7,
        "insiders": 9,
        "days": 6,
    }
    hr_w = max(20, len(header_hr) + 2)

    sep_row = (
        "+"
        + "-" * col_w["ticker"]
        + "+"
        + "-" * col_w["score"]
        + "+"
        + "-" * col_w["insiders"]
        + "+"
        + "-" * col_w["days"]
        + "+"
        + "-" * hr_w
        + "+"
    )

    title = f" TrailingEdge  INSIDER CLUSTER SIGNAL  {as_of_date} "
    title_width = len(sep_row) - 2
    click.echo("+" + "-" * title_width + "+")
    click.echo("|" + title.center(title_width) + "|")
    click.echo(sep_row)
    click.echo(
        "|"
        + " TICKER".ljust(col_w["ticker"])
        + "|"
        + " SCORE".ljust(col_w["score"])
        + "|"
        + " INSIDERS".ljust(col_w["insiders"])
        + "|"
        + " DAYS".ljust(col_w["days"])
        + "|"
        + " HIST. HIT RATE".ljust(hr_w)
        + "|"
    )
    click.echo(sep_row)

    for c in clusters:
        days_since = (as_of_date - c.window_end).days
        score_str = f"{float(c.cluster_score):.1f}"
        hit_str = "  ".join(f"{h}d:{_fmt_pct(base_rates[h].hit_rate_pct)}" for h in horizons)
        click.echo(
            "|"
            + f" {c.ticker}".ljust(col_w["ticker"])
            + "|"
            + f" {score_str}".ljust(col_w["score"])
            + "|"
            + f" {c.insider_count}".ljust(col_w["insiders"])
            + "|"
            + f" {days_since}".ljust(col_w["days"])
            + "|"
            + f" {hit_str}".ljust(hr_w)
            + "|"
        )

    click.echo(sep_row)
    _print_cost_warning()


# The base rate is computed GROSS. compute_base_rate knows nothing about the bid-ask
# spread, so on this sample it returns EDGE_DETECTED - a real, significant, and entirely
# uncapturable +2.07% at 20 days. Printing a hit rate and a verdict with no mention of the
# cost that eats them would have this tool contradict its own project's finding, and would
# be the single most misleading thing it could do: a reader sees "EDGE_DETECTED, 54.7%"
# and trades it. The measured net result goes on the same screen as the gross one.
_NET_OF_COST_20D = "-1.35%"
_NET_T_20D = "-3.31"


def _print_cost_warning() -> None:
    click.echo("")
    click.echo("  HIT RATE AND BASE RATE ABOVE ARE GROSS - BEFORE TRANSACTION COSTS.")
    click.echo(
        f"  Net of the measured round-trip cost, this signal has historically LOST money:"
        f" 20d net {_NET_OF_COST_20D} (t = {_NET_T_20D}, N = 1,070)."
    )
    click.echo("  Insider clusters fire in illiquid small caps whose spread is wider than")
    click.echo("  the alpha. See docs/METHODOLOGY.md and scripts/net_of_cost.py.")
    click.echo("")


def _to_json_safe(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, date):
        return str(obj)
    if isinstance(obj, list):
        return [_to_json_safe(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _to_json_safe(v) for k, v in obj.items()}
    return obj


async def generate_daily_report(as_of_date: date | None = None) -> DailyReport:
    """
    1. detect_clusters(as_of_date) - upsert all relevant cluster events
    2. calculate_outcomes(clusters, horizons) - fill forward returns
    3. compute_base_rate per horizon - ticker-agnostic historical accuracy
    4. Sort clusters by cluster_score DESC
    5. Print stdout table
    6. Write reports/daily/{YYYY-MM-DD}_signal.json
    """
    today = as_of_date or date.today()
    cfg = get_config()["signals"]
    horizons: list[int] = cfg["returns"]["horizons"]

    clusters = await detect_clusters(as_of_date=today)
    await calculate_outcomes(clusters, horizons)

    base_rates: dict[int, BaseRateStats] = {}
    for h in horizons:
        base_rates[h] = await compute_base_rate(h)

    # Sort by cluster_score DESC
    clusters_sorted = sorted(clusters, key=lambda c: c.cluster_score, reverse=True)

    if clusters_sorted:
        _print_table(today, clusters_sorted, base_rates)
    else:
        print(f"No active clusters as of {today}.")

    # Write JSON
    reports_dir = Path("reports") / "daily"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"{today}_signal.json"

    payload = {
        "as_of_date": str(today),
        "clusters": [
            {
                "ticker": c.ticker,
                "cluster_score": float(c.cluster_score),
                "insider_count": c.insider_count,
                "window_start": str(c.window_start),
                "window_end": str(c.window_end),
                "days_since_last_buy": (today - c.window_end).days,
                "unique_insiders": c.unique_insiders,
                "total_buy_value_try": float(c.total_buy_value_try) if c.total_buy_value_try else None,
            }
            for c in clusters_sorted
        ],
        # Returns are market-adjusted (AR = stock - benchmark over the same held
        # interval). Every figure ships with its interval, its test, and a verdict;
        # a consumer reading hit_rate_pct without reading verdict is reading noise.
        "base_rates": {
            str(h): {
                "horizon_days": h,
                "benchmark_ticker": base_rates[h].benchmark_ticker,
                "total_signals": base_rates[h].total_signals,
                "signals_with_outcome": base_rates[h].signals_with_outcome,
                "verdict": base_rates[h].verdict,
                "required_n_for_power": base_rates[h].required_n_for_power,
                "hit_rate_pct": float(base_rates[h].hit_rate_pct),
                "hit_rate_ci_95": [
                    float(base_rates[h].hit_rate_ci_low_pct),
                    float(base_rates[h].hit_rate_ci_high_pct),
                ],
                "mean_abnormal_return_pct": float(base_rates[h].mean_abnormal_return_pct),
                "median_abnormal_return_pct": float(base_rates[h].median_abnormal_return_pct),
                "t_stat": float(base_rates[h].t_stat),
                "p_value": float(base_rates[h].p_value),
                "best_abnormal_return_pct": float(base_rates[h].best_abnormal_return_pct),
                "worst_abnormal_return_pct": float(base_rates[h].worst_abnormal_return_pct),
                # Audit only - not evidence. See signals/abnormal.py.
                "mean_raw_return_pct": float(base_rates[h].mean_raw_return_pct),
                "mean_benchmark_return_pct": float(base_rates[h].mean_benchmark_return_pct),
            }
            for h in horizons
        },
        # Every base_rate above is GROSS. A machine consumer reading `verdict:
        # EDGE_DETECTED` and acting on it would be acting on a number this project has
        # measured as uncapturable, so the net result travels in the same document rather
        # than in a README the consumer never reads.
        "cost_adjusted": {
            "basis": "Abdi-Ranaldo (2017) spread + Kyle/Almgren impact + commission/BSMV,"
            " estimated per trade from the stock's own OHLC",
            "median_round_trip_cost_pct": 1.93,
            "mean_round_trip_cost_pct": 3.37,
            "net_abnormal_return_pct": {"5": -2.71, "20": -1.35, "60": -1.20},
            "net_t_stat": {"5": -12.91, "20": -3.31, "60": -1.86},
            "n": 1070,
            "verdict": "LOSES_MONEY_NET_OF_COST",
            "note": "The gross verdict above is real and significant. It is not tradeable:"
            " insider clusters fire in illiquid small caps whose bid-ask spread is wider"
            " than the alpha. See docs/METHODOLOGY.md.",
        },
        "generated_at": str(today),
    }
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _log.info("daily_report_written", path=str(report_path), cluster_count=len(clusters_sorted))

    return DailyReport(
        as_of_date=today,
        clusters=clusters_sorted,
        base_rates=base_rates,
        report_path=report_path,
    )
