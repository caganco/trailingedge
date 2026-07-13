"""Unit tests for KAP parsers using real fixture files."""
from datetime import date
from decimal import Decimal

import trailing_edge.scrapers.kap.parser as parser_mod
from trailing_edge.core.time import parse_kap_date
from trailing_edge.scrapers.kap.parser import (
    _COLUMN_ALIASES,
    find_column_index,
    parse_dkb_transactions,
    parse_oda_transactions,
    parse_turkish_number,
)
from trailing_edge.scrapers.kap.types import RelationType


def test_dkb_pdf_extracts_nasmed_transaction(dkb_pdf_bytes):
    txs = parse_dkb_transactions(dkb_pdf_bytes, ticker="EGEPO")
    assert len(txs) >= 1, "Expected at least one transaction row"

    tx = txs[0]
    assert tx.insider_name == "SERVET NASIR"
    assert tx.transaction_date == date(2026, 5, 25)
    assert tx.transaction_type == "SELL"
    assert tx.share_count == Decimal("2500000")
    # Price range 18.45 - 18.48 TRY; lower bound stored
    assert tx.price_try is not None
    assert Decimal("18") < tx.price_try < Decimal("19")
    # Post-tx ownership ~19.1%
    assert tx.post_tx_ownership_pct is not None
    assert abs(tx.post_tx_ownership_pct - Decimal("19.1")) < Decimal("0.2")


def test_turkish_number_normalization():
    assert parse_turkish_number("1.234.567,89") == Decimal("1234567.89")
    assert parse_turkish_number("18,45") == Decimal("18.45")
    assert parse_turkish_number("0") == Decimal("0")
    assert parse_turkish_number("2.500.000") == Decimal("2500000")


def test_turkish_date_normalization():
    assert parse_kap_date("25/05/2026") == date(2026, 5, 25)
    assert parse_kap_date("2026-05-25") == date(2026, 5, 25)
    assert parse_kap_date("25.05.2026") == date(2026, 5, 25)


def test_relation_type_empty_fields_is_kendisi(dkb_pdf_bytes):
    """NASMED fixture has all blank relation fields → should yield KENDISI."""
    txs = parse_dkb_transactions(dkb_pdf_bytes, ticker="EGEPO")
    assert txs
    assert txs[0].relation_type == RelationType.KENDISI


def test_missing_price_is_none_not_zero(dkb_pdf_bytes):
    """
    If no price range appears in the PDF, price_try must be None, not Decimal('0').
    We test this by confirming that a price extracted from the NASMED fixture is
    either None or a positive value - never exactly zero.
    """
    txs = parse_dkb_transactions(dkb_pdf_bytes, ticker="EGEPO")
    assert txs
    for tx in txs:
        assert tx.price_try != Decimal("0"), "price_try must be None when absent, not 0"


def test_oda_parser_does_not_crash(oda_html):
    """ODA HTML parser must return a list (possibly empty) without raising."""
    result = parse_oda_transactions(oda_html, ticker="PEKGY")
    assert isinstance(result, list)


def test_row_with_implausible_ownership_pct_is_rejected_whole(monkeypatch, dkb_pdf_bytes):
    """An out-of-range percentage means the columns are misaligned - the WHOLE row goes.

    The previous policy nulled the percentage and kept the rest of the row. That is
    how 21% of stored transactions ended up with silently wrong share counts: a row
    that has already proven its columns shifted cannot be trusted in any field.
    Rejecting loses one recoverable row; keeping it poisons every statistic built on
    top. This test pins the stricter contract.
    """
    orig = parser_mod._extract_table_rows

    def _inject_implausible(text: str):
        rows = orig(text)
        for row in rows:
            if len(row) > 8:
                row[8] = "5.260.000"
        return rows

    monkeypatch.setattr(parser_mod, "_extract_table_rows", _inject_implausible)
    txs = parse_dkb_transactions(dkb_pdf_bytes, ticker="TEST")
    assert txs == [], "a row with an impossible percentage must be dropped, not patched"


def test_column_mapping_missing_header_returns_none():
    assert find_column_index(["Alım", "Satım", "Net"], ["Bilinmiyor Kolon"]) is None


def test_column_mapping_partial_match():
    headers = [
        "Alım Pay Adedi",
        "Satım Pay Adedi",
        "İşlem Sonrası Sahip Olunan Pay Oranı (%)",
    ]
    idx = find_column_index(headers, _COLUMN_ALIASES["post_tx_ownership_pct"])
    assert idx == 2
