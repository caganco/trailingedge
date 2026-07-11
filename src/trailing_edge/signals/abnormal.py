"""Market-adjusted (abnormal) return model.

Why market adjustment is not optional
-------------------------------------
A raw forward return is not evidence of a signal. BIST quotes nominal TRY
returns under high inflation, so the unconditional drift of a randomly chosen
stock over a 20-60 trading-day window is materially positive - a "hit rate"
measured against a 50% coin-flip prior, or a mean measured against 0%, credits
market drift and beta to the signal. The quantity that has to clear zero is the
return *in excess of the market over the same held interval*:

    AR = R_stock - R_benchmark

Model choice (market-adjusted, not market model)
------------------------------------------------
The market-adjusted model fixes alpha=0 and beta=1 rather than estimating them
over a pre-event window. Two reasons this is the right call here, not a shortcut:

1. Kothari & Warner (2007), "Econometrics of Event Studies": for SHORT-horizon
   event studies, test-statistic specification is not highly sensitive to the
   benchmark model of normal returns. Estimating beta buys little and costs an
   estimation window - which, for thinly traded BIST names, is precisely where
   beta is least reliable.
2. Estimating beta per event on illiquid small caps (the population insider
   clusters concentrate in) would inject noise, and non-synchronous trading
   biases OLS beta downward.

The 60-day horizon is on the boundary of "short"; Kothari & Warner note that
long-horizon tests have low power and are benchmark-sensitive. Treat the 60-day
number as weaker evidence than the 5/20-day numbers, not as a stronger one.

Benchmark choice
----------------
XU100 (BIST 100). Not an arbitrary pick: a review of 75 Borsa Istanbul event
studies finds BIST-100 with market-adjusted returns to be the field's standard
market proxy. Using the house benchmark keeps results comparable to the
published literature instead of to a private convention.

Known limitation (stated, not hidden): XU100 is a large-cap index, while insider
clusters concentrate in smaller names. A size-matched benchmark would be a
better control for the size premium. XU100 is the literature default and the
honest first cut; a size-matched portfolio is the documented next step, not a
silent assumption.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_QUANT = Decimal("0.0001")
_HUNDRED = Decimal("100")


def pct_return(entry: Decimal, exit_: Decimal) -> Decimal:
    """Simple percentage return from entry to exit. Raises on non-positive entry."""
    if entry <= 0:
        raise ValueError(f"entry price must be positive, got {entry}")
    return ((exit_ - entry) / entry * _HUNDRED).quantize(_QUANT, rounding=ROUND_HALF_UP)


def abnormal_return(
    stock_entry: Decimal,
    stock_exit: Decimal,
    bench_entry: Decimal,
    bench_exit: Decimal,
) -> Decimal:
    """
    Market-adjusted abnormal return, in percent: AR = R_stock - R_benchmark.

    Both legs must be measured over the SAME held calendar interval - the caller
    is responsible for pricing the benchmark on the stock's actual entry/exit
    dates (see ``get_price_and_date_after_days``). Passing benchmark prices taken
    at the benchmark's own trading-day offsets would compare mismatched windows
    whenever the stock was halted.
    """
    r_stock = pct_return(stock_entry, stock_exit)
    r_bench = pct_return(bench_entry, bench_exit)
    return (r_stock - r_bench).quantize(_QUANT, rounding=ROUND_HALF_UP)
