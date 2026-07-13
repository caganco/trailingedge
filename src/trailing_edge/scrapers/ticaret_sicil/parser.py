"""Parsers for Ticaret Sicil Gazetesi.

Two stages:
1. parse_search_results - HTML table from ilangoruntuleme.php → ilan rows
   (company, sicil no, date, gazette issue, PDF guid).
2. parse_gazette_ocr - OCR text of a gazette page → structured record for ONE
   target company. A gazette PDF is a full page containing MULTIPLE companies'
   notices, so we split into per-notice blocks and fuzzy-match the target
   company name to pick the correct block. This prevents attributing another
   company's directors to the target.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from trailing_edge.scrapers.ticaret_sicil.matcher import normalize_name_tr


@dataclass
class IlanRow:
    """One row from the gazette search results table."""

    company_name: str
    sicil_no: str | None
    city: str | None
    gazette_date: date | None
    gazette_issue: str | None  # "Sayı"
    page_no: str | None        # "Sayfa"
    ilan_type: str | None
    pdf_guid: str              # for pdf_goster.php?Guid=...


@dataclass
class RawPerson:
    name: str          # raw OCR name (un-normalized - DB stores original)
    role: str | None   # "Yönetim Kurulu Üyesi", "Temsile Yetkili", ...


@dataclass
class GazetteRecord:
    company_name: str
    city: str | None = None
    founded_date: date | None = None
    gazette_issue: str | None = None
    source_url: str | None = None
    persons: list[RawPerson] = field(default_factory=list)
    raw_text: str = ""


# ── Search results ──────────────────────────────────────────────────────────

_PDF_GUID_RE = re.compile(r"pdf_goster\.php\?Guid=([a-z0-9\-]+)")


def _parse_tr_date(s: str) -> date | None:
    s = s.strip()
    m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if not m:
        return None
    d, mo, y = (int(x) for x in m.groups())
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def parse_search_results(html: str) -> list[IlanRow]:
    """Parse the gazette search results table into ilan rows.

    Empty / no-results HTML → []. Columns (ilangoruntuleme.php):
    Müdürlük | Sicil No | Unvan | Yayın Tarihi | Sayı | Sayfa | İlan Türü | Gazete(PDF)
    """
    soup = BeautifulSoup(html, "html.parser")
    rows: list[IlanRow] = []

    for tr in soup.select("tbody tr[role=row]"):
        tds = tr.find_all("td")
        if len(tds) < 8:
            continue

        pdf_link = tr.find("a", href=_PDF_GUID_RE)
        if not pdf_link:
            continue
        # BeautifulSoup types an attribute as str | AttributeValueList (a multi-valued
        # attribute like class=""); href is single-valued, but the type says otherwise.
        guid_match = _PDF_GUID_RE.search(str(pdf_link["href"]))
        if not guid_match:
            continue

        company = tds[2].get_text(strip=True)
        if not company:
            continue

        rows.append(
            IlanRow(
                company_name=company,
                city=tds[0].get_text(strip=True) or None,
                sicil_no=tds[1].get_text(strip=True) or None,
                gazette_date=_parse_tr_date(tds[3].get_text(strip=True)),
                gazette_issue=tds[4].get_text(strip=True) or None,
                page_no=tds[5].get_text(strip=True) or None,
                ilan_type=tds[6].get_text(strip=True) or None,
                pdf_guid=guid_match.group(1),
            )
        )

    return rows


# ── Gazette OCR ─────────────────────────────────────────────────────────────

# Each notice on a gazette page starts with "İlan Sıra No".
_BLOCK_SPLIT_RE = re.compile(r"(?=İlan\s+Sıra\s+No)")

# Company name inside a block: an uppercase line ending in a company-type suffix.
_COMPANY_RE = re.compile(
    r"([A-ZÇĞİÖŞÜ][A-ZÇĞİÖŞÜ0-9\s\.\-]{8,}?"
    r"(?:ANONİM\s+ŞİRKETİ|LİMİTED\s+ŞİRKETİ|Ş[İI]RKET[İI]|ŞTİ))"
)

# Person + role: "...ikamet eden, AD SOYAD; DD.MM.YYYY tarihine kadar ROLE olarak seçilmiştir"
_PERSON_RE = re.compile(
    r"ikamet eden,?\s+([A-ZÇĞİÖŞÜ][A-ZÇĞİÖŞÜ\s]+?)[;,]?\s*"
    r"\d{1,2}\.\d{1,2}\.\d{4}\s*tarihine kadar\s*"
    r"(.*?)\s*olarak seçilmiştir"
)

# Leading OCR artifacts on the company line ("İ İ İ COMPANY...").
_LEADING_ARTIFACT_RE = re.compile(r"^(?:[İI]\s+)+")


def _clean_company(raw: str) -> str:
    cleaned = _LEADING_ARTIFACT_RE.sub("", raw)
    return re.sub(r"\s+", " ", cleaned).strip()


def _extract_company(block_norm: str) -> str | None:
    m = _COMPANY_RE.search(block_norm)
    return _clean_company(m.group(1)) if m else None


def _extract_persons(block_norm: str) -> list[RawPerson]:
    seen: set[tuple[str, str]] = set()
    persons: list[RawPerson] = []
    for m in _PERSON_RE.finditer(block_norm):
        name = re.sub(r"\s+", " ", m.group(1)).strip()
        role = re.sub(r"\s+", " ", m.group(2)).strip() or None
        key = (name.casefold(), (role or "").casefold())
        if key in seen:
            continue
        seen.add(key)
        persons.append(RawPerson(name=name, role=role))
    return persons


def parse_gazette_ocr(
    ocr_text: str,
    target_company: str,
    threshold: float = 0.60,
) -> GazetteRecord | None:
    """Extract the notice block for `target_company` from a gazette page.

    The page holds several companies' notices. We split into per-notice blocks,
    fuzzy-match each block's company name against `target_company`, and return a
    record built ONLY from the best-matching block. Returns None if no block
    clears `threshold` - refusing to attribute the wrong company's directors.
    """
    if not ocr_text.strip():
        return None

    target_norm = normalize_name_tr(target_company)

    blocks = [b for b in _BLOCK_SPLIT_RE.split(ocr_text) if b.strip()]

    best_score = 0.0
    best_block_norm: str | None = None
    best_company: str | None = None

    for block in blocks:
        block_norm = re.sub(r"\s+", " ", block)
        company = _extract_company(block_norm)
        if not company:
            continue
        score = fuzz.token_set_ratio(target_norm, normalize_name_tr(company)) / 100.0
        if score > best_score:
            best_score, best_block_norm, best_company = score, block_norm, company

    if best_block_norm is None or best_score < threshold:
        return None

    return GazetteRecord(
        company_name=best_company or target_company,
        persons=_extract_persons(best_block_norm),
        raw_text=best_block_norm.strip(),
    )
