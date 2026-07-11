"""Market-adjusted return model."""
from decimal import Decimal

import pytest

from trailing_edge.signals.abnormal import abnormal_return, pct_return


def test_pct_return_basic():
    assert pct_return(Decimal("100"), Decimal("110")) == Decimal("10.0000")
    assert pct_return(Decimal("100"), Decimal("90")) == Decimal("-10.0000")


def test_pct_return_rejects_non_positive_entry():
    with pytest.raises(ValueError):
        pct_return(Decimal("0"), Decimal("10"))


def test_abnormal_return_strips_the_market():
    # Stock +10%, market +10% -> the stock did nothing the market did not.
    ar = abnormal_return(
        stock_entry=Decimal("100"),
        stock_exit=Decimal("110"),
        bench_entry=Decimal("1000"),
        bench_exit=Decimal("1100"),
    )
    assert ar == Decimal("0.0000")


def test_a_rising_stock_can_have_a_negative_abnormal_return():
    """The whole point of the fix: +8% is a LOSS when the market gave +12%.

    The old pipeline reported this as a 'hit' (return > 0). Under nominal TRY
    returns on BIST this is the common case, not a corner case.
    """
    ar = abnormal_return(
        stock_entry=Decimal("100"),
        stock_exit=Decimal("108"),
        bench_entry=Decimal("1000"),
        bench_exit=Decimal("1120"),
    )
    assert ar == Decimal("-4.0000")
    assert ar < 0
    assert pct_return(Decimal("100"), Decimal("108")) > 0  # raw return says "win"


def test_abnormal_return_beats_a_falling_market():
    # Stock -2%, market -10% -> genuine outperformance.
    ar = abnormal_return(
        stock_entry=Decimal("100"),
        stock_exit=Decimal("98"),
        bench_entry=Decimal("1000"),
        bench_exit=Decimal("900"),
    )
    assert ar == Decimal("8.0000")
