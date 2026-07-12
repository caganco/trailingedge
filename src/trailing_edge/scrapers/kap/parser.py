"""Dual-path parser: DKB (PDF) and ODA (HTML) disclosure types."""
from __future__ import annotations

import io
import re
from decimal import Decimal, InvalidOperation

from trailing_edge.core.logging import get_logger
from trailing_edge.core.time import parse_kap_date
from trailing_edge.scrapers.kap.types import KapDisclosureDTO, KapInsiderTxDTO, RelationType

_log = get_logger(__name__)

# Matches Turkish formatted numbers: 1.234.567,89 or 1.234 or 18,45 or -2.500.000
_TR_NUM_RE = re.compile(r"-?[\d]{1,3}(?:\.[\d]{3})*(?:,[\d]+)?")

# KAP writes the transaction date BOTH ways depending on the filing's vintage:
#   14.06.2019  (dots)    - filings up to ~2020
#   07/06/2023  (slashes) - filings from ~2021
# This pattern used to accept slashes only, so no row in a pre-2021 filing ever anchored
# and _extract_table_rows returned nothing: every filing before 2021 parsed to ZERO
# transactions while the ingest reported success. The table was always there - measured
# side by side, the 2019 and 2023 layouts carry the identical 9-column row - and the only
# difference was this separator. parse_kap_date already accepted both formats; only this
# regex was turning four years of history away.
#
# A dotted date cannot be confused with a Turkish number: _TR_NUM_RE requires groups of
# exactly 3 digits after a dot, and "14.06.2019" has 2.
_DATE_RE = re.compile(r"\b(\d{2}[./]\d{2}[./]\d{4})\b")

_PRICE_RANGE_RE = re.compile(r"([\d]+[,.][\d]+)\s*-\s*([\d]+[,.][\d]+)\s*TL")

# Known Turkish column header aliases per logical field (for header-driven column detection).
_COLUMN_ALIASES: dict[str, list[str]] = {
    "post_tx_ownership_pct": [
        "İşlem Sonrası Sahip Olunan",
        "Sahip Olunan Pay Oranı",
        "İşlem Sonrası Pay Oranı",
        "Pay Oranı",
    ],
    "share_count": ["Pay Adedi", "İşlem Adedi", "Nominal Değer"],
    "price_try": ["Fiyat", "İşlem Fiyatı", "Fiyat Aralığı"],
    "transaction_type": ["İşlem Türü", "Alım/Satım"],
    "transaction_date": ["İşlem Tarihi", "Tarih"],
    "insider_name": ["Adı Soyadı", "Ad Soyad", "Kişi"],
}


def find_column_index(headers: list[str], aliases: list[str]) -> int | None:
    """Return first index in headers where any alias is a substring (case-insensitive)."""
    for i, h in enumerate(headers):
        for alias in aliases:
            if alias.lower() in h.lower():
                return i
    return None


def parse_turkish_number(s: str) -> Decimal:
    """'1.234.567,89' → Decimal('1234567.89'), '18,45' → Decimal('18.45')."""
    s = s.strip()
    negative = s.startswith("-")
    s = s.lstrip("-")
    s = s.replace(".", "").replace(",", ".")
    val = Decimal(s)
    return -val if negative else val


def _extract_table_rows(text: str) -> list[list[str]]:
    """
    Find date-anchored table rows in the pdfminer text stream.
    Each row starts with a DD/MM/YYYY date followed by 9 numeric tokens.
    """
    # Flatten to a clean token list
    tokens: list[str] = []
    # One line is normally one cell. But pdfminer sometimes emits two adjacent table cells
    # on a single line ("174.004.552,79 174.269.552,79"). As one token that fails
    # _TR_NUM_RE, and the collector below silently *skips* non-numeric tokens - so two
    # values vanish, every later column shifts left by two, and post_tx_share_count /
    # post_tx_ownership_pct are read out of the wrong cells. That is worse than a crash:
    # it yields plausible-looking wrong numbers (the source of the
    # implausible_ownership_pct warnings, e.g. an ownership percentage of 24.628.606,69).
    #
    # Split such a line, but ONLY when every part is a Turkish number. Splitting
    # unconditionally would shred narrative text too - "18,45 - 18,48 TL" from the price
    # sentence would yield a numeric token "18,45" that the collector would happily read
    # as a table column. Requiring the whole line to be numeric keeps prose out.
    for line in text.splitlines():
        line = line.strip().replace("\xa0", "")
        if not line:
            continue
        parts = line.split()
        if len(parts) > 1 and all(_TR_NUM_RE.fullmatch(p.lstrip("-")) for p in parts):
            tokens.extend(parts)
        else:
            tokens.append(line)

    rows: list[list[str]] = []
    i = 0
    while i < len(tokens):
        m = _DATE_RE.fullmatch(tokens[i])
        if m:
            date_tok = tokens[i]
            # Collect up to 9 numeric tokens after the date
            numerics: list[str] = []
            j = i + 1
            while j < len(tokens) and len(numerics) < 9:
                tok = tokens[j]
                if _TR_NUM_RE.fullmatch(tok.lstrip("-")):
                    numerics.append(tok)
                elif tok and not _DATE_RE.fullmatch(tok):
                    pass  # skip non-numeric, non-date tokens
                else:
                    break
                j += 1
            if len(numerics) >= 3:
                rows.append([date_tok] + numerics)
                i = j
                continue
        i += 1
    return rows


def _find_insider_name(text: str) -> str:
    """Extract insider name from 'Ad Soyad / ...' label section."""
    # The label is garbled by encoding; look for the pattern: label\n:\n[spaces]NAME
    # The name is on a line of its own after the colon, padded with \xa0
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        # "Ad Soyad" appears in the label, name follows after ":"
        if "Ad Soyad" in line or "SERVET" in line.upper():
            # look ahead for a line that looks like a name (all caps, multiple words)
            for k in range(idx, min(idx + 6, len(lines))):
                candidate = lines[k].strip().replace("\xa0", "").strip()
                if candidate and candidate.isupper() and len(candidate.split()) >= 2:
                    return candidate
    # Fallback: scan for all-caps multi-word name after a colon
    colon_value_re = re.compile(r":\s*\xa0*\s*([A-ZÇĞİÖŞÜ][A-ZÇĞİÖŞÜ\s]{3,})")
    m = colon_value_re.search(text)
    if m:
        return m.group(1).strip()
    return "UNKNOWN"


def _find_ticker(detail_json: dict) -> str:
    stocks = detail_json.get("disclosureBasic", {}).get("relatedStocks", [])
    if stocks:
        return stocks[0].get("stock", "").upper()
    return ""


def _relation_type_from_context(text: str) -> str:
    """
    Return relation type by inspecting the relation field block only.
    The three relation fields appear as :\n\n:\n\n:\n\n followed by their values.
    When all blank (only whitespace/\\xa0) → KENDISI.
    """
    # Isolate only the relation-value block (between triple colons and next page/section)
    m = re.search(r":\n\n:\n\n:\n\n(.{0,500}?)(?:\x0c|\d{2}/\d{2}/\d{4})", text, re.DOTALL)
    if m:
        block = m.group(1)
        non_blank = re.sub(r"[\s\xa0]", "", block)
        if not non_blank:
            return RelationType.KENDISI
        # Non-blank relation values: classify
        if re.search(r"(?i)(eş|çocuk|spouse|child|kardeş|anne|baba)", block):
            return RelationType.YAKINI
        if re.search(r"(?i)(a\.\s*ş\.|ltd\.|anonim|limited)", block):
            return RelationType.ILISKILI_TUZEL_KISI
        return RelationType.YAKINI  # Non-empty but unclassified → assume related person
    # No triple-colon block found → default KENDISI (self-transaction)
    return RelationType.KENDISI


def parse_dkb_transactions(
    pdf_bytes: bytes,
    ticker: str = "",
    insider_name: str = "",
) -> list[KapInsiderTxDTO]:
    """Extract transactions from a Java-unwrapped DKB PDF."""
    from pdfminer.high_level import extract_text

    text = extract_text(io.BytesIO(pdf_bytes))

    if not insider_name:
        insider_name = _find_insider_name(text)

    relation_type = _relation_type_from_context(text)

    # Extract price range from narrative
    price_try: Decimal | None = None
    pm = _PRICE_RANGE_RE.search(text)
    if pm:
        try:
            price_try = parse_turkish_number(pm.group(1))
        except InvalidOperation:
            pass

    rows = _extract_table_rows(text)
    txs: list[KapInsiderTxDTO] = []

    for row in rows:
        try:
            tx_date = parse_kap_date(row[0])
            # columns: date, buy_nominal, sell_nominal, net, start_nom, end_nom,
            #          start_cap_pct, start_vote_pct, end_cap_pct, end_vote_pct
            buy_nominal = parse_turkish_number(row[1]) if len(row) > 1 else Decimal(0)
            sell_nominal = parse_turkish_number(row[2]) if len(row) > 2 else Decimal(0)

            if sell_nominal > 0 and buy_nominal == 0:
                tx_type = "SELL"
                share_count = sell_nominal
            elif buy_nominal > 0 and sell_nominal == 0:
                tx_type = "BUY"
                share_count = buy_nominal
            elif sell_nominal > 0:
                tx_type = "SELL"
                share_count = sell_nominal
            else:
                tx_type = "BUY"
                share_count = buy_nominal

            end_nom: Decimal | None = None
            if len(row) > 5:
                try:
                    end_nom = parse_turkish_number(row[5])
                except InvalidOperation:
                    pass

            end_cap_pct: Decimal | None = None
            if len(row) > 8:
                try:
                    end_cap_pct = parse_turkish_number(row[8])
                except InvalidOperation:
                    pass
            if end_cap_pct is not None and not (0 <= end_cap_pct <= 100):
                _log.warning("implausible_ownership_pct", raw=row[8], parsed=float(end_cap_pct))
                end_cap_pct = None

            txs.append(
                KapInsiderTxDTO(
                    insider_name=insider_name,
                    relation_type=relation_type,
                    ticker=ticker,
                    transaction_date=tx_date,
                    transaction_type=tx_type,
                    share_count=share_count,
                    price_try=price_try,
                    post_tx_share_count=end_nom,
                    post_tx_ownership_pct=end_cap_pct,
                )
            )
        except Exception as exc:
            _log.warning("dkb_row_parse_error", row=row, error=str(exc))

    return txs


def parse_disclosure_metadata(
    detail_json: dict,
    list_item: dict | None = None,
) -> KapDisclosureDTO:
    """
    Extract KapDisclosureDTO from the unwrapped /attachment-detail/{index} response.

    detail_json: the single element from the API list, already unwrapped.
    list_item: optional original list-API item (provides disclosureClass=DKB).
    """
    from trailing_edge.core.config import get_config
    from trailing_edge.core.time import parse_kap_datetime

    cfg = get_config()
    base_url = cfg["kap"]["base_url"]

    # The detail response nests metadata under disclosure.disclosureBasic
    disc_wrap = detail_json.get("disclosure", detail_json)
    basic = disc_wrap.get("disclosureBasic", disc_wrap)

    disclosure_index = str(basic.get("disclosureIndex", ""))
    # relatedStocks is a plain string ticker in the real API (not a list of dicts)
    related = basic.get("relatedStocks", "")
    ticker = (related.strip().upper() if isinstance(related, str) else "")
    if not ticker and list_item:
        related_li = list_item.get("relatedStocks", "")
        ticker = related_li.strip().upper() if isinstance(related_li, str) else ""

    company = (
        basic.get("companyTitle", "")
        or basic.get("title", "")
        or (list_item or {}).get("kapTitle", "")
        or ""
    )

    published_str = basic.get("publishDate", "") or (list_item or {}).get("publishDate", "")
    published_at = parse_kap_datetime(published_str) if published_str else None

    # Use disclosureClass from the list API (DKB) because the detail API returns DUY
    disclosure_class = (list_item or {}).get("disclosureClass") or basic.get("disclosureClass", "DKB")

    return KapDisclosureDTO(
        kap_disclosure_id=disclosure_index,  # use stable numeric index as ID
        ticker=ticker,
        company_name=company,
        disclosure_type=basic.get("summary", "") or basic.get("title", ""),
        disclosure_subtype=basic.get("disclosureType", None),
        disclosure_class=disclosure_class,
        published_at=published_at,
        is_correction=bool(basic.get("isChanged") or False),
        source_url=f"{base_url}/tr/bildirim/{disclosure_index}",
        raw_json=detail_json,
    )


def parse_oda_transactions(html: str, ticker: str = "") -> list[KapInsiderTxDTO]:
    """Best-effort ODA HTML parser. Returns [] on any failure - Phase 2 priority."""
    try:
        from selectolax.parser import HTMLParser

        tree = HTMLParser(html)
        table = tree.css_first("table")
        if not table:
            _log.warning("oda_no_table_found")
            return []
        # ODA transactions are fund threshold crossings, not individual insider trades.
        # Parsing is Phase 2 - return empty list for now.
        _log.info("oda_parse_skipped", reason="phase2")
        return []
    except Exception as exc:
        _log.warning("oda_parse_error", error=str(exc))
        return []
