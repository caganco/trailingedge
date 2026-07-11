"""Statistical gates in base_rate: interval, test, and power.

These lock in the reason the old reporting was misleading. The point estimate was
never the problem - publishing it without an interval was.
"""
import math

import pytest

from trailing_edge.signals.base_rate import (
    required_n,
    t_test_vs_zero,
    wilson_interval,
)


def test_the_original_n29_result_cannot_reject_the_null():
    """The committed sample: 20d, N=29, hit rate 55.17% (16/29).

    The 95% Wilson interval must straddle 50%. This is the whole finding: that
    number was never evidence of anything.
    """
    lo, hi = wilson_interval(hits=16, n=29)
    assert lo < 0.50 < hi
    assert lo == pytest.approx(0.372, abs=0.01)
    assert hi == pytest.approx(0.717, abs=0.01)


def test_required_n_for_a_55pct_hit_rate():
    """n = (1.96 + 0.84)^2 * 0.25 / 0.05^2 ~= 784. N=29 is not close."""
    n = required_n(p_null=0.50, p_alt=0.55)
    assert 780 <= n <= 790
    assert n > 29 * 25


def test_a_bigger_sample_narrows_the_interval():
    lo_small, hi_small = wilson_interval(hits=16, n=29)
    lo_big, hi_big = wilson_interval(hits=1600, n=2900)
    assert (hi_big - lo_big) < (hi_small - lo_small)
    assert lo_big > 0.50  # at N=2900 the same 55% IS distinguishable


def test_wilson_handles_degenerate_samples():
    assert wilson_interval(0, 0) == (0.0, 0.0)
    lo, hi = wilson_interval(0, 10)
    assert lo == 0.0 and 0.0 < hi < 1.0


def test_t_test_detects_no_edge_in_noise():
    # Symmetric returns around zero -> no significance.
    values = [1.0, -1.0, 2.0, -2.0, 0.5, -0.5] * 5
    t, p = t_test_vs_zero(values)
    assert abs(t) < 1e-9
    assert p > 0.05


def test_t_test_detects_a_real_mean_shift():
    values = [3.0, 2.5, 3.5, 2.8, 3.2] * 20  # tight, clearly positive
    t, p = t_test_vs_zero(values)
    assert t > 1.96
    assert p < 0.05


def test_t_test_is_safe_on_tiny_and_constant_samples():
    assert t_test_vs_zero([]) == (0.0, 1.0)
    assert t_test_vs_zero([1.0]) == (0.0, 1.0)
    assert t_test_vs_zero([2.0, 2.0, 2.0]) == (0.0, 1.0)  # zero variance


def test_p_value_matches_the_normal_tail():
    values = [1.0] * 99 + [-1.0]  # mean ~0.98, sd ~0.2
    t, p = t_test_vs_zero(values)
    assert p == pytest.approx(math.erfc(abs(t) / math.sqrt(2)), rel=1e-9)
