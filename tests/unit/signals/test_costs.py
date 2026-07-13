"""Round-trip cost model - the module that decides the project's answer.

The headline result turns on this: a gross abnormal return of +1.76% at 20 days against a
median round-trip cost of 1.94%. If the cost were wrong in either direction the conclusion
would flip, so its failure modes are pinned here rather than trusted.
"""
from decimal import Decimal

from trailing_edge.signals.costs import (
    COMMISSION_PCT,
    abdi_ranaldo_spread_pct,
    kyle_impact_pct,
    round_trip_cost,
    tick_floor_pct,
)


def _series(n: int, base: float = 10.0, wobble: float = 0.02) -> tuple[list, list, list]:
    """A synthetic OHLC series whose closes sit off the high-low midpoint - the pattern
    Abdi-Ranaldo reads a spread from."""
    closes, highs, lows = [], [], []
    for i in range(n):
        mid = base * (1 + 0.001 * i)
        # close alternates above/below the midpoint: bid-ask bounce
        c = mid * (1 + wobble if i % 2 else 1 - wobble)
        closes.append(Decimal(str(round(c, 4))))
        highs.append(Decimal(str(round(mid * 1.03, 4))))
        lows.append(Decimal(str(round(mid * 0.97, 4))))
    return closes, highs, lows


# --- tick floor -------------------------------------------------------------

def test_tick_floor_is_coarser_for_cheap_stocks():
    """BIST's grid is 0.01 TRY below 20 TRY. A 3 TRY small cap therefore cannot be
    quoted tighter than ~33bp however quiet its prints look - which is the whole reason
    a 'zero spread' estimate must not be believed."""
    assert tick_floor_pct(Decimal("3")) == Decimal("0.01") / Decimal("3")
    assert tick_floor_pct(Decimal("100")) == Decimal("0.02") / Decimal("100")
    assert tick_floor_pct(Decimal("3")) > tick_floor_pct(Decimal("100"))


def test_tick_floor_of_zero_price_is_zero_not_infinite():
    assert tick_floor_pct(Decimal("0")) == 0


# --- spread -----------------------------------------------------------------

def test_spread_is_detected_from_bid_ask_bounce():
    closes, highs, lows = _series(40)
    spread = abdi_ranaldo_spread_pct(closes, highs, lows)
    assert spread is not None
    assert spread > Decimal("0.005")  # well above the tick floor at this price


def test_a_quiet_window_is_floored_at_one_tick_not_dropped():
    """The trap that cost 96% of the sample.

    When the window is quiet the estimator's gamma goes >= 0 and the formula reports a
    spread of exactly zero. Treating that as 'cannot estimate' and excluding the trade
    selects on precisely the price behaviour the signal is about: the survivors' gross
    return came out NEGATIVE where the full sample's was positive. A quiet window is not
    a free trade - it is floored, never dropped.
    """
    n = 40
    closes = [Decimal("10")] * n  # perfectly flat: gamma cannot be negative
    highs = [Decimal("10")] * n
    lows = [Decimal("10")] * n

    spread = abdi_ranaldo_spread_pct(closes, highs, lows)

    assert spread is not None, "a quiet window must not void the estimate"
    assert spread == tick_floor_pct(Decimal("10"))


def test_spread_returns_none_only_when_there_is_no_history():
    assert abdi_ranaldo_spread_pct([], [], []) is None


def test_tick_floor_keys_off_the_traded_price_not_the_adjusted_index():
    """price_history.close_try is a chained total-return index, not a price. A serial
    bonus-issuer's index can sit far above its actual print - 118x at the extreme on
    2018-12 data. The tick floor is 0.01 TRY on the exchange's grid, so keying it off the
    index divides the floor by that factor and hands the trade a spread it could never
    get. The estimator is scale-free and stays on the index; the floor takes the price.
    """
    n = 40
    # a quiet window, so the estimate IS the floor and nothing else can mask the error
    closes = [Decimal("100")] * n  # index level
    highs = [Decimal("100")] * n
    lows = [Decimal("100")] * n

    traded = Decimal("2.50")  # what the stock actually prints after its bonus issues

    on_index = abdi_ranaldo_spread_pct(closes, highs, lows)
    on_price = abdi_ranaldo_spread_pct(closes, highs, lows, last_traded_price=traded)

    assert on_index == tick_floor_pct(Decimal("100"))  # 0.02/100 = 2bp - fiction
    assert on_price == tick_floor_pct(traded)  # 0.01/2.50 = 40bp - the real grid
    assert on_price > on_index * 10


def test_a_bad_price_inside_the_estimation_window_voids_the_estimate():
    closes, highs, lows = _series(40)
    closes[-3] = Decimal("0")  # inside the trailing 21-session window
    assert abdi_ranaldo_spread_pct(closes, highs, lows) is None


def test_a_bad_price_outside_the_window_is_irrelevant():
    """The estimator reads only the trailing window, so a fault before it cannot corrupt
    the estimate - and must not be allowed to void an otherwise sound one. Voiding here
    would drop the trade, and dropping trades on a price-data condition is how the cost
    model selected 96% of the sample away the first time."""
    closes, highs, lows = _series(40)
    closes[2] = Decimal("0")  # far outside the trailing 21 sessions
    assert abdi_ranaldo_spread_pct(closes, highs, lows) is not None


# --- impact -----------------------------------------------------------------

def test_impact_scales_with_square_root_of_size():
    closes, _, _ = _series(40)
    adv = Decimal("1000000")
    small = kyle_impact_pct(closes, Decimal("10000"), adv)
    large = kyle_impact_pct(closes, Decimal("40000"), adv)
    # 4x the order -> 2x the impact
    assert large == small * 2 or abs(large - small * 2) < Decimal("0.0001")


def test_impact_is_negligible_at_retail_size():
    """The one cost a small account genuinely escapes - and showing that it is small is
    the point, because it means the spread is doing all the damage."""
    closes, _, _ = _series(40)
    impact = kyle_impact_pct(closes, Decimal("25000"), Decimal("5000000"))
    assert impact < Decimal("0.005")  # under 50bp one-sided


def test_impact_is_zero_when_adv_is_unknown():
    closes, _, _ = _series(40)
    assert kyle_impact_pct(closes, Decimal("25000"), Decimal("0")) == 0


# --- round trip -------------------------------------------------------------

def test_round_trip_charges_the_spread_once_and_impact_twice():
    """Crossing the spread costs it in full on each side, so it enters at 1x. Impact is
    paid separately on entry and exit, so it enters at 2x."""
    closes, highs, lows = _series(40)
    rt = round_trip_cost(closes, highs, lows, Decimal("25000"), Decimal("1000000"))
    assert rt is not None

    spread = abdi_ranaldo_spread_pct(closes, highs, lows)
    impact = kyle_impact_pct(closes, Decimal("25000"), Decimal("1000000"))
    expected = (spread + 2 * impact + 2 * COMMISSION_PCT * Decimal("1.05")) * 100

    assert abs(rt.total_pct - expected) < Decimal("0.001")


def test_round_trip_is_none_only_without_price_history():
    assert round_trip_cost([], [], [], Decimal("25000"), Decimal("1000000")) is None


def test_an_illiquid_name_costs_more_than_the_alpha():
    """The result, in one test. A wide-spread small cap cannot be round-tripped for less
    than the +1.76% gross abnormal return the signal produces at 20 days."""
    n = 40
    closes, highs, lows = [], [], []
    for i in range(n):
        mid = 3.0
        c = mid * (1.04 if i % 2 else 0.96)  # heavy bounce = wide spread
        closes.append(Decimal(str(round(c, 4))))
        highs.append(Decimal("3.12"))
        lows.append(Decimal("2.88"))

    rt = round_trip_cost(closes, highs, lows, Decimal("25000"), Decimal("200000"))

    assert rt is not None
    assert rt.total_pct > Decimal("1.76"), "the spread must eat the alpha, as measured"
