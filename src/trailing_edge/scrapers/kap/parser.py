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
            # Collect up to 9 numeric tokens after the date. In a real table row the
            # cells are consecutive, so tolerate at most a few non-numeric interruptions
            # (page-break artifacts) - an unbounded skip used to let the collector wander
            # arbitrarily far into the document and stitch together numbers from
            # unrelated narrative, which is where the 2015-era garbage rows came from.
            numerics: list[str] = []
            skips_left = 3
            j = i + 1
            while j < len(tokens) and len(numerics) < 9:
                tok = tokens[j]
                if _TR_NUM_RE.fullmatch(tok.lstrip("-")):
                    numerics.append(tok)
                elif tok and not _DATE_RE.fullmatch(tok):
                    skips_left -= 1
                    if skips_left < 0:
                        break
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


# The canonical DKB table row: date + exactly these 9 numeric cells, in this order.
# Verified identical on 2019 (dotted dates) and 2023+ (slashed dates) filings.
#   [0] buy nominal   [1] sell nominal   [2] |net|
#   [3] start nominal [4] end nominal
#   [5] start capital % [6] start vote % [7] end capital % [8] end vote %
_ROW_NUMERIC_COUNT = 9
# Display values are exact decimals; the tolerance only absorbs rounding of the
# printed cells, not real mismatches.
_NOMINAL_TOL = Decimal("1")


def _map_canonical_row(numerics: list[str]) -> tuple[dict | None, str]:
    """
    Map the 9 numeric cells of a table row to transaction fields - or reject.

    Position is the schema here (the form is fixed), but position alone is exactly
    what produced 21% silently-wrong rows: a missing or merged cell shifts every
    later column and the row still "parses", yielding share counts like 3.02 and
    ownership percentages in the millions. So the mapping must PROVE the positions
    are right before anything is stored, using the arithmetic the form itself
    guarantees:

        start_nominal + (buy - sell) == end_nominal
        |buy - sell| == |net|
        every percentage in [0, 100]

    A shifted layout essentially cannot satisfy the first identity by accident.
    Rows that fail are rejected with a reason - a rejected row is recoverable from
    the logs, a silently corrupt one poisons every statistic built on top of it.
    """
    if len(numerics) != _ROW_NUMERIC_COUNT:
        return None, f"expected {_ROW_NUMERIC_COUNT} cells, got {len(numerics)}"

    try:
        buy, sell, net, start_nom, end_nom = (parse_turkish_number(v) for v in numerics[:5])
        pcts = [parse_turkish_number(v) for v in numerics[5:9]]
    except InvalidOperation:
        return None, "unparseable numeric cell"

    if buy < 0 or sell < 0:
        return None, "negative buy/sell nominal"
    if buy == 0 and sell == 0:
        # A Pay Alim Satim Bildirimi row with no volume is a totals/summary line,
        # not a transaction. These used to be stored as BUY with share_count=0.
        return None, "zero volume (summary row)"

    signed_net = buy - sell
    if abs(abs(signed_net) - abs(net)) > _NOMINAL_TOL:
        return None, "net column disagrees with buy-sell"
    if abs(start_nom + signed_net - end_nom) > _NOMINAL_TOL:
        return None, "start+net != end (shifted columns?)"
    if any(p < 0 or p > 100 for p in pcts):
        return None, "percentage outside [0,100]"
    if signed_net == 0:
        # Equal intraday buy and sell: no position change, no directional signal.
        return None, "flat round-trip (net zero)"

    return {
        "transaction_type": "BUY" if signed_net > 0 else "SELL",
        "share_count": abs(signed_net),
        "post_tx_share_count": end_nom,
        "post_tx_ownership_pct": pcts[2],  # end capital %
    }, ""


# The 2015-2020 filing is a different document: a per-trade blotter
# (date | Alım/Satım | adet | fiyat | tutar | pre/post holdings), not the netted
# one-row-per-day form used from ~2021. pdfminer emits its cells in an unstable
# order - the 2015 fixture interleaves column-major and row-major within one
# table - so reconstructing individual trades positionally is exactly the kind of
# guesswork that produced silent corruption before. Two anchors in the document
# are order-independent and self-checking, and the parser uses only those:
#
#   1. The TOPLAM ALIŞ / TOPLAM SATIŞ summary block: the next four numerics are
#      (buy qty, sell qty, buy amount, sell amount), verified on both era
#      fixtures against the per-trade rows they summarise.
#   2. The narrative price range ("1,11 - 1,16 TL fiyat aralığından"): the
#      implied average price amount/qty must fall inside it.
#
# A per-filing net summary is also the honest granularity: the modern form nets
# same-day trades into one row anyway, and the signal pipeline keys on
# (insider, ticker, date, direction, size). Post-transaction holdings are NOT
# extractable order-independently, so they are left NULL rather than guessed.
_LEGACY_BUY_LABEL = "TOPLAM ALI"  # prefix-match: trailing Ş varies with encoding
_LEGACY_SELL_LABEL = "TOPLAM SATI"
_LEGACY_PRICE_MIN = Decimal("0.01")
_LEGACY_PRICE_MAX = Decimal("100000")


def _parse_legacy_blotter(
    text: str,
    ticker: str,
    insider_name: str,
    relation_type: str,
) -> list[KapInsiderTxDTO]:
    lines = [ln.strip().replace("\xa0", "") for ln in text.splitlines() if ln.strip()]

    sell_idx = next(
        (i for i, ln in enumerate(lines) if ln.upper().startswith(_LEGACY_SELL_LABEL)),
        None,
    )
    if sell_idx is None:
        _log.warning("dkb_row_rejected", reason="legacy: TOPLAM SATIŞ label not found")
        return []

    # First four numeric cells after the totals labels: buy qty, sell qty,
    # buy amount, sell amount (order verified on 2015 and 2018 fixtures).
    numerics: list[Decimal] = []
    for ln in lines[sell_idx + 1:]:
        for tok in ln.split():
            if _TR_NUM_RE.fullmatch(tok.lstrip("-")):
                try:
                    numerics.append(parse_turkish_number(tok))
                except InvalidOperation:
                    pass
        if len(numerics) >= 4:
            break
    if len(numerics) < 4:
        _log.warning("dkb_row_rejected", reason="legacy: totals block incomplete")
        return []
    buy_qty, sell_qty, buy_amt, sell_amt = numerics[:4]

    # Latest trade date in the document. The blotter's own rows and the narrative
    # carry the same dates, so a plain scan is order-independent.
    dates = []
    for tok in _DATE_RE.findall(text):
        try:
            dates.append(parse_kap_date(tok))
        except ValueError:
            pass
    if not dates:
        _log.warning("dkb_row_rejected", reason="legacy: no parseable trade date")
        return []
    tx_date = max(dates)

    # Narrative price range, if present, bounds the implied average price.
    range_lo = range_hi = None
    pm = _PRICE_RANGE_RE.search(text)
    if pm:
        try:
            range_lo = parse_turkish_number(pm.group(1))
            range_hi = parse_turkish_number(pm.group(2))
        except InvalidOperation:
            pass

    txs: list[KapInsiderTxDTO] = []
    for tx_type, qty, amt in (("BUY", buy_qty, buy_amt), ("SELL", sell_qty, sell_amt)):
        if qty == 0 and amt == 0:
            continue
        if qty <= 0 or amt <= 0:
            _log.warning(
                "dkb_row_rejected",
                reason=f"legacy: {tx_type} qty/amount inconsistent",
                qty=float(qty),
                amount=float(amt),
            )
            continue
        avg_price = (amt / qty).quantize(Decimal("0.0001"))
        if not (_LEGACY_PRICE_MIN <= avg_price <= _LEGACY_PRICE_MAX):
            _log.warning(
                "dkb_row_rejected",
                reason="legacy: implied price implausible",
                avg_price=float(avg_price),
            )
            continue
        if range_lo is not None and range_hi is not None and range_lo > 0:
            if not (range_lo * Decimal("0.9") <= avg_price <= range_hi * Decimal("1.1")):
                _log.warning(
                    "dkb_row_rejected",
                    reason="legacy: implied price outside narrative range",
                    avg_price=float(avg_price),
                    range_lo=float(range_lo),
                    range_hi=float(range_hi),
                )
                continue
        txs.append(
            KapInsiderTxDTO(
                insider_name=insider_name,
                relation_type=relation_type,
                ticker=ticker,
                transaction_date=tx_date,
                transaction_type=tx_type,
                share_count=qty,
                price_try=avg_price,
                post_tx_share_count=None,
                post_tx_ownership_pct=None,
            )
        )
        _log.info(
            "dkb_legacy_summary",
            ticker=ticker,
            tx_type=tx_type,
            qty=float(qty),
            avg_price=float(avg_price),
        )
    return txs


def parse_dkb_transactions(
    pdf_bytes: bytes,
    ticker: str = "",
    insider_name: str = "",
) -> list[KapInsiderTxDTO]:
    """Extract transactions from a Java-unwrapped DKB PDF.

    Two document generations, routed by an unambiguous marker: filings carrying a
    TOPLAM ALIŞ/SATIŞ totals block (2015-2020) go through the legacy blotter
    summary; everything else through the canonical one-row-per-day table. Every
    accepted row is validated against arithmetic the form itself guarantees;
    rows that fail are logged as dkb_row_rejected and dropped - never stored
    with guessed fields.
    """
    from pdfminer.high_level import extract_text

    text = extract_text(io.BytesIO(pdf_bytes))

    if not insider_name:
        insider_name = _find_insider_name(text)

    relation_type = _relation_type_from_context(text)

    # Legacy (2015-2020) blotter form: route by its unambiguous totals marker.
    # The modern canonical form never contains it (checked on both modern fixtures).
    if _LEGACY_BUY_LABEL in text.upper():
        return _parse_legacy_blotter(text, ticker, insider_name, relation_type)

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
        except ValueError as exc:
            _log.warning("dkb_row_rejected", reason=str(exc), row=row)
            continue

        fields, reason = _map_canonical_row(row[1:])
        if fields is None:
            _log.warning("dkb_row_rejected", reason=reason, row=row)
            continue

        txs.append(
            KapInsiderTxDTO(
                insider_name=insider_name,
                relation_type=relation_type,
                ticker=ticker,
                transaction_date=tx_date,
                price_try=price_try,
                **fields,
            )
        )

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
