"""Legacy (2015-2020) blotter parsing, against real KAP PDFs committed as fixtures.

The expected values below are read off the documents themselves, not off the parser:
the 2015 TEKFEN filing narrates six buys totalling 120,000 shares for 526,950 TL, and
the 2018 METRO filing narrates 479,593 bought (1.11-1.16 TL) and 69,368 sold. The
TOPLAM block in each PDF carries exactly those totals, and qty x price == amount holds
per trade - so if the parser disagrees with these numbers, the parser is wrong.
"""
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from trailing_edge.scrapers.kap.parser import parse_dkb_transactions

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "kap"


@pytest.fixture(scope="module")
def era2015_pdf() -> bytes:
    return (FIXTURES / "dkb_era2015.pdf").read_bytes()


@pytest.fixture(scope="module")
def era2018_pdf() -> bytes:
    return (FIXTURES / "dkb_era2018.pdf").read_bytes()


def test_2015_tekfen_filing_nets_to_one_buy(era2015_pdf):
    """Six intraday buys (10+20+10+20+25+35 = 120 thousand) net to one BUY summary."""
    txs = parse_dkb_transactions(era2015_pdf, ticker="TKFEN")

    assert len(txs) == 1
    tx = txs[0]
    assert tx.transaction_type == "BUY"
    assert tx.share_count == Decimal("120000")
    assert tx.transaction_date == date(2015, 6, 15)
    # 526.950 / 120.000 = 4,39125 TL - inside the traded range 4,36..4,41
    assert Decimal("4.36") <= tx.price_try <= Decimal("4.41")


def test_2015_holdings_are_null_not_guessed(era2015_pdf):
    """pdfminer emits this table in an unstable interleaved order, so post-transaction
    holdings cannot be recovered order-independently. NULL is the honest value -
    the previous parser stored numbers from whatever cells it happened to land on."""
    tx = parse_dkb_transactions(era2015_pdf, ticker="TKFEN")[0]
    assert tx.post_tx_share_count is None
    assert tx.post_tx_ownership_pct is None


def test_2018_metro_filing_yields_buy_and_sell(era2018_pdf):
    """A filing with both directions produces two summary transactions."""
    txs = parse_dkb_transactions(era2018_pdf, ticker="METRO")

    assert len(txs) == 2
    by_type = {t.transaction_type: t for t in txs}

    buy = by_type["BUY"]
    assert buy.share_count == Decimal("479593")
    # 549.526,49 / 479.593 = 1,1458 - inside the narrated 1,11-1,16 range
    assert Decimal("1.11") <= buy.price_try <= Decimal("1.16")

    sell = by_type["SELL"]
    assert sell.share_count == Decimal("69368")
    assert sell.price_try == Decimal("1.13")  # 78.385,84 / 69.368 exactly

    assert all(t.transaction_date == date(2018, 6, 13) for t in txs)


def test_2015_insider_name_is_extracted(era2015_pdf):
    txs = parse_dkb_transactions(era2015_pdf, ticker="TKFEN")
    assert "YATIRIM HOLD" in txs[0].insider_name.upper()


def test_modern_fixture_does_not_take_the_legacy_path(dkb_pdf_bytes):
    """The 2026 EGEPO fixture has no TOPLAM block; it must still parse canonically
    with full holdings data - the legacy route must not swallow modern filings."""
    txs = parse_dkb_transactions(dkb_pdf_bytes, ticker="EGEPO")
    assert txs
    assert txs[0].post_tx_ownership_pct is not None
