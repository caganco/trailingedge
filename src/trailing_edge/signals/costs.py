"""Round-trip transaction cost for an insider-cluster trade.

Why this decides the question
-----------------------------
The base rate reports a +1.46% abnormal return at 5 days and +2.04% at 20 days,
before costs. Whether either survives is not a detail - it IS the question. And it
is a hard question here precisely because the signal fires where trading is most
expensive: insider clusters concentrate in illiquid BIST small caps, exactly the
names with the widest spreads.

So the cost has to be estimated PER TRADE from that stock's own price action, not
assumed as a flat fee. A flat 0.2% would flatter the result; the spread on a thin
name is routinely several times that.

The model
---------
    round_trip = 2 x (commission + BSMV) + spread + 2 x impact

- Spread: Abdi & Ranaldo (2017), "A Simple Estimation of Bid-Ask Spreads from Daily
  Close, High, and Low Prices", RFS 30(12). It recovers the effective spread from
  OHLC alone, which is all the exchange bulletin gives us - and it gives it for
  delisted names too, where no quote data survives at all.

      c_t   = ln(close_t)
      m_t   = (ln(high_t) + ln(low_t)) / 2
      gamma = mean[(c_t - m_t)(c_{t-1} - m_t)]   over the window
      spread = 2 sqrt(max(-gamma, 0))

  Crossing the spread once on entry and once on exit costs it in full, which is why
  it enters the round trip at 1x, not 2x.

- Impact: Kyle (1985) / Almgren et al. (2005) square-root law,
  impact = lambda * sigma_daily * sqrt(order_value / ADV), paid on each side.
  At retail size (order << ADV) this is small - and showing that it is small is the
  point, because it is the one cost that a small account genuinely escapes.

- Commission is charged per side; BSMV (5%) applies to the commission itself.
  Gains tax is zero for a domestic individual under Gecici-67, so it is not modelled.

Every number is an estimate and the direction of error matters: an underestimated
cost turns a null into an edge. Where the data is too thin to estimate a spread at
all, the caller gets None rather than a cheap default.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

# BIST retail defaults. Deliberately mid-range rather than best-case: a cost model
# tuned to the cheapest broker on the friendliest name is a way of assuming the answer.
COMMISSION_PCT = Decimal("0.0015")  # 15 bps per side
BSMV_RATE = Decimal("0.05")  # levied on the commission
KYLE_LAMBDA = Decimal("1.0")  # uncalibrated; see kyle_impact_pct

_SPREAD_WINDOW = 21


@dataclass(frozen=True)
class RoundTrip:
    spread_pct: Decimal
    impact_pct: Decimal
    commission_pct: Decimal
    total_pct: Decimal


def _gamma(
    closes: list[Decimal], highs: list[Decimal], lows: list[Decimal], window: int
) -> float | None:
    n = min(len(closes), len(highs), len(lows))
    if n < window + 1:
        return None
    c_s, h_s, l_s = closes[-(window + 1) :], highs[-(window + 1) :], lows[-(window + 1) :]

    diffs: list[float] = []
    for c, h, low in zip(c_s, h_s, l_s, strict=True):
        if c <= 0 or h <= 0 or low <= 0:
            return None
        mid = (math.log(float(h)) + math.log(float(low))) / 2
        diffs.append(math.log(float(c)) - mid)

    products = [diffs[i] * diffs[i - 1] for i in range(1, len(diffs))]
    if not products:
        return None
    return sum(products) / len(products)


def tick_floor_pct(price: Decimal) -> Decimal:
    """Smallest spread the exchange's price grid permits, as a fraction.

    A stock cannot be quoted tighter than one tick, so this is the hard floor under any
    spread estimate. BIST's equity tick is 0.01 TRY below 20 TRY and 0.02 TRY above -
    the coarse grid is why a 3 TRY small cap cannot have a spread under ~33bp however
    quiet its prints look.
    """
    if price <= 0:
        return Decimal(0)
    tick = Decimal("0.01") if price < 20 else Decimal("0.02")
    return tick / price


def abdi_ranaldo_spread_pct(
    closes: list[Decimal],
    highs: list[Decimal],
    lows: list[Decimal],
    window: int = _SPREAD_WINDOW,
) -> Decimal | None:
    """Effective bid-ask spread as a fraction, from OHLC alone (Abdi-Ranaldo 2017).

    The estimator produces gamma >= 0 whenever a window happens to be quiet, and the
    formula then reports a spread of exactly zero. Treating that as "cannot estimate"
    and dropping the trade is a trap I walked into: it discarded 96% of the sample, and
    the survivors' gross abnormal return came out NEGATIVE where the full sample's was
    positive. The dropout is not random - it selects on precisely the price behaviour
    the signal is about, so the exclusion silently inverts the answer.

    A quiet window is not evidence of a free trade. So a degenerate gamma widens the
    window rather than voiding the estimate, and whatever survives is floored at one
    tick - the tightest quote the exchange's own price grid allows. None is returned
    only when there is genuinely not enough price history to look at.
    """
    for w in (window, 60, 120):
        g = _gamma(closes, highs, lows, w)
        if g is None:
            continue
        if g < 0:
            spread = 2 * math.sqrt(-g)
            est = Decimal(str(spread))
            break
    else:
        est = Decimal(0)

    if not closes:
        return None
    if _gamma(closes, highs, lows, min(window, max(len(closes) - 1, 2))) is None:
        return None

    floor = tick_floor_pct(closes[-1])
    return max(est, floor).quantize(Decimal("0.000001"))


def kyle_impact_pct(
    closes: list[Decimal],
    order_value_try: Decimal,
    adv_try: Decimal,
    window: int = _SPREAD_WINDOW,
    lambda_kyle: Decimal = KYLE_LAMBDA,
) -> Decimal:
    """One-sided square-root market impact.

    impact = lambda * sigma_daily * sqrt(order_value / ADV)

    lambda is an uncalibrated 1.0 - calibrating it needs execution data this project
    does not have. At retail order sizes the term is small enough that the error does
    not change any conclusion; at institutional size it would, and the number should
    not be trusted there.
    """
    if adv_try <= 0 or order_value_try <= 0 or len(closes) < 3:
        return Decimal(0)

    tail = closes[-(window + 1) :]
    rets = [
        math.log(float(tail[i]) / float(tail[i - 1]))
        for i in range(1, len(tail))
        if tail[i] > 0 and tail[i - 1] > 0
    ]
    if len(rets) < 2:
        return Decimal(0)

    mean = sum(rets) / len(rets)
    sigma = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
    impact = float(lambda_kyle) * sigma * math.sqrt(float(order_value_try / adv_try))
    return Decimal(str(impact)).quantize(Decimal("0.000001"))


def round_trip_cost(
    closes: list[Decimal],
    highs: list[Decimal],
    lows: list[Decimal],
    order_value_try: Decimal,
    adv_try: Decimal,
) -> RoundTrip | None:
    """Total cost of entering and exiting one position, as a percent of notional.

    None when the spread cannot be estimated: a trade whose cost is unknown must be
    excluded from the sample, not priced at zero.
    """
    spread = abdi_ranaldo_spread_pct(closes, highs, lows)
    if spread is None:
        return None

    impact = kyle_impact_pct(closes, order_value_try, adv_try)
    commission = 2 * COMMISSION_PCT * (1 + BSMV_RATE)

    total = spread + 2 * impact + commission
    return RoundTrip(
        spread_pct=spread * 100,
        impact_pct=impact * 100,
        commission_pct=commission * 100,
        total_pct=(total * 100).quantize(Decimal("0.0001")),
    )
