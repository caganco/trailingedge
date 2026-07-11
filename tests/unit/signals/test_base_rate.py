"""BaseRateStats construction and the verdict gate (pure computation, no DB)."""
from decimal import Decimal

from trailing_edge.signals.base_rate import (
    _ZERO,
    INSUFFICIENT_POWER,
    BaseRateStats,
    _empty,
)


def test_empty_stats_refuse_to_claim_anything():
    stats = _empty(horizon_days=20, total=0, need=784)
    assert stats.signals_with_outcome == 0
    assert stats.hit_rate_pct == Decimal("0.00")
    assert stats.verdict == INSUFFICIENT_POWER
    assert stats.required_n_for_power == 784


def test_empty_stats_are_emitted_even_when_rows_exist_but_are_unpriced():
    """Clusters exist but no benchmark price -> still INSUFFICIENT_POWER, not a
    silent zero. An unpriced benchmark must never be read as 'no abnormal return'."""
    stats = _empty(horizon_days=5, total=42, need=784)
    assert stats.total_signals == 42
    assert stats.signals_with_outcome == 0
    assert stats.verdict == INSUFFICIENT_POWER
    assert stats.p_value == Decimal("1.0000")


def test_stats_carry_an_interval_and_a_test_not_just_a_point_estimate():
    """The dataclass must not be constructible without the honesty fields - a hit
    rate with no interval is what made the old reporting misleading."""
    fields = BaseRateStats.__dataclass_fields__
    for required in (
        "hit_rate_ci_low_pct",
        "hit_rate_ci_high_pct",
        "t_stat",
        "p_value",
        "required_n_for_power",
        "verdict",
        "mean_abnormal_return_pct",
    ):
        assert required in fields, f"BaseRateStats lost its {required} field"


def test_raw_return_is_kept_but_clearly_secondary():
    """Raw and benchmark returns stay available for audit, so a reader can see how
    much of a headline number was simply the market."""
    fields = BaseRateStats.__dataclass_fields__
    assert "mean_raw_return_pct" in fields
    assert "mean_benchmark_return_pct" in fields


def test_zero_sentinel_is_a_decimal():
    assert _ZERO == Decimal("0.00")
