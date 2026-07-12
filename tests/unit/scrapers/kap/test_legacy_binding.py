"""Totals-binding disambiguation and price parsing in the legacy blotter.

The first clean-backfill attempt rejected ~65% of 2015-era filings. The reasons
tallied to three concrete defects, each pinned here with a synthetic document:

  18x "implied price outside narrative range" - the price regex read "1.234,56 TL"
      as 1234 (grabbing "1.234") and dot-decimal "4.36" as 436; and a single
      narrated BUY range was applied to the SELL side too.
  18x "qty/amount inconsistent" - pdfminer emits the TOPLAM cells column-major in
      some files and row-major in others; assuming one order broke the other.
  10x "totals block incomplete" - still rejected loudly (needs the cells to exist).
"""
from decimal import Decimal

from trailing_edge.scrapers.kap.parser import (
    _PRICE_RANGE_RE,
    _parse_legacy_blotter,
    _parse_price_token,
)


def _doc(totals_lines: str, narrative: str = "") -> str:
    return f"""SÜREKLİ BİLGİLERE İLİŞKİN ÖZEL DURUM AÇIKLAMASI
{narrative}
İşlem Tarihi
15.06.2015
Alım
TOPLAM ALIŞ
TOPLAM SATIŞ
{totals_lines}
"""


def parse(doc: str):
    return _parse_legacy_blotter(doc, ticker="X", insider_name="A B", relation_type="KENDISI")


# --- price token parsing -----------------------------------------------------

def test_price_token_thousands_and_comma():
    assert _parse_price_token("1.234,56") == Decimal("1234.56")


def test_price_token_dot_decimal():
    """Old filings write '4.36' meaning 4 lira 36 kurus - not 436."""
    assert _parse_price_token("4.36") == Decimal("4.36")


def test_price_token_plain_comma():
    assert _parse_price_token("18,45") == Decimal("18.45")


def test_range_regex_captures_full_thousands_price():
    m = _PRICE_RANGE_RE.search("1.234,56 - 1.240,00 TL fiyat aralığından")
    assert m.group(1) == "1.234,56"
    assert _parse_price_token(m.group(1)) == Decimal("1234.56")


# --- totals binding ----------------------------------------------------------

def test_column_major_binding():
    """(buy qty, sell qty, buy amt, sell amt) - the 2015/2018 fixture order."""
    txs = parse(_doc("120.000\n0\n526.950\n0"))
    assert len(txs) == 1
    assert txs[0].transaction_type == "BUY"
    assert txs[0].share_count == Decimal("120000")


def test_row_major_binding_is_recognised():
    """(buy qty, buy amt, sell qty, sell amt) - the emission that used to be
    rejected as 'qty/amount inconsistent'. 120.000 @ 526.950 implies 4.39 TL;
    read column-major it implies buy amt 0 for qty 120.000, which fails - so
    exactly one binding survives and it is the right one."""
    txs = parse(_doc("120.000\n526.950\n0\n0"))
    assert len(txs) == 1
    assert txs[0].transaction_type == "BUY"
    assert txs[0].share_count == Decimal("120000")
    assert txs[0].price_try == Decimal("4.3913")


def test_truly_ambiguous_binding_is_rejected():
    """Four cells that pass BOTH bindings with different substance must not be
    half-trusted. (100 sh @ 200 TL vs 100 sh @ 300 TL depending on order.)"""
    txs = parse(_doc("100\n200\n300\n400"))
    assert txs == []


def test_single_buy_range_does_not_reject_the_sell_side():
    """One narrated range (the buy leg's). Sell at 9,50 is outside 1,11-1,16 -
    that must NOT reject the sell: only the buy is checked against the buy range."""
    doc = _doc(
        "479.593\n69.368\n549.526,49\n658.996",  # sell avg 9.5 TL
        narrative="1,11 - 1,16 TL fiyat aralığından 479.593 adet alış işlemi",
    )
    txs = parse(doc)
    assert {t.transaction_type for t in txs} == {"BUY", "SELL"}


def test_two_ranges_bind_buy_first_sell_last():
    doc = _doc(
        "1.000\n2.000\n4.500\n19.000",  # buy avg 4,50 ; sell avg 9,50
        narrative=(
            "4,40 - 4,60 TL fiyat aralığından 1.000 adet alış işlemi ve "
            "9,40 - 9,60 TL fiyat aralığından 2.000 adet satış işlemi"
        ),
    )
    txs = parse(doc)
    assert len(txs) == 2
    by = {t.transaction_type: t for t in txs}
    assert by["BUY"].price_try == Decimal("4.5000")
    assert by["SELL"].price_try == Decimal("9.5000")


def test_buy_price_outside_its_own_range_still_rejects():
    """The range check must keep its teeth: a buy avg of 45 TL against a narrated
    4,40-4,60 range is a mis-binding and the filing must be rejected."""
    doc = _doc(
        "1.000\n0\n45.000\n0",
        narrative="4,40 - 4,60 TL fiyat aralığından 1.000 adet alış işlemi",
    )
    assert parse(doc) == []


def test_incomplete_totals_still_rejected():
    assert parse(_doc("120.000\n526.950")) == []


# --- separated labels (each label owns its pair) ------------------------------

def test_separated_labels_bind_per_label():
    """The emission behind 31 'totals block incomplete' rejections in month one:
    TOPLAM ALIŞ / qty / amt / TOPLAM SATIŞ / qty / amt. Looking only after the
    SELL label finds the sell side's lonely zeros and nothing else.

    Cells verbatim from quarantined filing 405865: 395.321 shares bought for
    256.442,84 TL (avg 0,6487), nothing sold."""
    doc = """SÜREKLİ BİLGİLERE İLİŞKİN ÖZEL DURUM AÇIKLAMASI
İşlem Tarihi
05.01.2015
Alım
TOPLAM ALIŞ
395.321
256.442,84
TOPLAM SATIŞ
0
0
"""
    txs = parse(doc)
    assert len(txs) == 1
    assert txs[0].transaction_type == "BUY"
    assert txs[0].share_count == Decimal("395321")
    assert txs[0].price_try == Decimal("0.6487")


def test_separated_labels_with_both_sides_active():
    doc = """SÜREKLİ BİLGİLERE İLİŞKİN ÖZEL DURUM AÇIKLAMASI
İşlem Tarihi
05.01.2015
TOPLAM ALIŞ
1.000
4.500
TOPLAM SATIŞ
2.000
19.000
"""
    txs = parse(doc)
    by = {t.transaction_type: t for t in txs}
    assert by["BUY"].share_count == Decimal("1000")
    assert by["SELL"].share_count == Decimal("2000")
    assert by["SELL"].price_try == Decimal("9.5000")


def test_separated_labels_missing_sell_pair_rejected():
    doc = """başlık
05.01.2015
TOPLAM ALIŞ
1.000
4.500
TOPLAM SATIŞ
"""
    assert parse(doc) == []
