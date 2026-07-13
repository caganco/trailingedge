"""_map_canonical_row: the arithmetic proof that the columns are what we think.

Position is the schema in a DKB table, and position alone produced 21% silently-wrong
rows in production (share_count=0 summary lines, percentages stored as share counts,
ownership percentages in the millions). The validator uses the identities the form
itself guarantees - start + (buy - sell) == end, |buy-sell| == |net|, percentages in
[0,100] - so a shifted layout cannot survive by accident.
"""
from decimal import Decimal

from trailing_edge.scrapers.kap.parser import _map_canonical_row

# The NASMED fixture row, verbatim: sell of 2.5M, holdings 98M -> 95.5M.
GOOD_SELL = ["0", "2.500.000", "-2.500.000", "98.000.000", "95.500.000",
             "19,6", "22,25", "19,1", "21,99"]
# A real 2019-style buy: 107.266.000 + 1.845.337 = 109.111.337.
GOOD_BUY = ["1.845.337", "0", "1.845.337", "107.266.000", "109.111.337",
            "39,34", "39,34", "40,02", "40,02"]


def test_valid_sell_row_maps():
    fields, reason = _map_canonical_row(GOOD_SELL)
    assert reason == ""
    assert fields["transaction_type"] == "SELL"
    assert fields["share_count"] == Decimal("2500000")
    assert fields["post_tx_share_count"] == Decimal("95500000")
    assert fields["post_tx_ownership_pct"] == Decimal("19.1")


def test_valid_buy_row_maps():
    fields, _ = _map_canonical_row(GOOD_BUY)
    assert fields["transaction_type"] == "BUY"
    assert fields["share_count"] == Decimal("1845337")


def test_short_row_is_rejected():
    """A partial row is precisely the case fixed indices used to mis-map."""
    fields, reason = _map_canonical_row(GOOD_BUY[:5])
    assert fields is None
    assert "expected 9" in reason


def test_zero_volume_summary_row_is_rejected():
    """Production had 4 stored transactions with share_count=0 - totals lines."""
    fields, reason = _map_canonical_row(
        ["0", "0", "0", "1.000.000", "1.000.000", "5", "5", "5", "5"]
    )
    assert fields is None
    assert "zero volume" in reason


def test_shifted_columns_fail_the_arithmetic_and_are_rejected():
    """Drop one cell and shift the rest left - the exact production corruption.

    The row still has plausible-looking numbers everywhere, which is why it survived
    before. start + net == end is what unmasks it.
    """
    shifted = GOOD_BUY[1:] + ["40,02"]  # sell slid into buy's slot, etc.
    fields, reason = _map_canonical_row(shifted)
    assert fields is None


def test_net_column_disagreement_is_rejected():
    row = list(GOOD_BUY)
    row[2] = "999"  # |net| != |buy - sell|
    fields, reason = _map_canonical_row(row)
    assert fields is None
    assert "net column" in reason


def test_percentage_out_of_range_rejects_the_whole_row():
    """Production stored ownership percentages like 25.923.015,31 - a share count
    that landed in a percentage column. The row must go entirely: if one column is
    provably misaligned, share_count is not trustworthy either."""
    row = list(GOOD_BUY)
    row[7] = "25.923.015,31"
    fields, reason = _map_canonical_row(row)
    assert fields is None
    assert "percentage" in reason


def test_flat_roundtrip_is_rejected():
    """Equal buy and sell: no position change, no directional information."""
    fields, reason = _map_canonical_row(
        ["500", "500", "0", "1.000.000", "1.000.000", "5", "5", "5", "5"]
    )
    assert fields is None
    assert "net zero" in reason


def test_mixed_day_nets_to_the_dominant_side():
    """Intraday buy AND sell on one row: the honest direction is the net one.

    (The old code called any row with sell>0 a SELL, even if buys dominated.)
    """
    fields, _ = _map_canonical_row(
        ["3.000", "1.000", "2.000", "10.000", "12.000", "1", "1", "1,2", "1,2"]
    )
    assert fields["transaction_type"] == "BUY"
    assert fields["share_count"] == Decimal("2000")


def test_negative_nominal_is_rejected():
    row = list(GOOD_BUY)
    row[0] = "-1.845.337"
    fields, reason = _map_canonical_row(row)
    assert fields is None
    assert "negative" in reason
