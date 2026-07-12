# TrailingEdge

> **Asynchronous Python data engine that ingests SPK II-15.1 insider
> transaction disclosures from KAP (kap.org.tr), measures empirical
> forward returns, and produces per-company insider-activity briefs for
> BIST-listed companies.**

[![Python](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org)
[![PostgreSQL](https://img.shields.io/badge/postgres-16-336791.svg)](https://www.postgresql.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![CI](https://github.com/caganco/trailingedge/actions/workflows/ci.yml/badge.svg)](https://github.com/caganco/trailingedge/actions/workflows/ci.yml)
[![mypy](https://img.shields.io/badge/mypy-checked-2a6db2.svg)](https://mypy-lang.org/)
[![Ruff](https://img.shields.io/badge/ruff-checked-d7ff64.svg)](https://github.com/astral-sh/ruff)

---

## What it does

- **Scrapes** Turkey's public-disclosure platform (KAP) for SPK II-15.1
  individual-insider transaction reports - Turkey's regulatory equivalent
  of SEC Form 4.
- **Reverse-engineers** KAP's undocumented Java-serialized `byte[]` PDF
  wrapper, parses the DKB transaction tables (Turkish-locale numbers,
  Windows-1252 encoded), and stores normalised rows in PostgreSQL with
  cryptographic deduplication.
- **Measures market-adjusted abnormal returns** (5/20/60 trading-day horizons)
  over detected insider-cluster events. Entry is t+1 after the KAP disclosure is
  *public* - never the private transaction date - and every return is measured in
  excess of XU100 over the same held interval. Estimates ship with a Wilson
  interval, a t-test, and a power gate that returns `INSUFFICIENT_POWER` rather
  than a number it cannot support. See [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md).
- **Generates forensic briefs** (HTML + PDF) per BIST ticker, combining
  insider-transaction history, board-interlock graphs, and (optionally)
  Türkiye Ticaret Sicil Gazetesi cross-references.

> **No edge is claimed.** This is measurement infrastructure, not a strategy. The
> sample sizes reached so far are far below what is needed to distinguish an edge
> from chance (~784 events; see `reports/sample/README.md`), and no transaction
> cost or VBTS tradability filter is applied yet - so any positive figure this
> pipeline produces is an upper bound, before frictions. The
> [open issues](docs/METHODOLOGY.md#5-known-open-issues) are documented rather
> than left for the reader to find.

## Türkçe özet

TrailingEdge, BIST'in **SPK II-15.1 (Pay Alım Satım Bildirimi)**
kapsamındaki şirket-içi alım-satım bildirimlerini KAP üzerinden çekip
PostgreSQL'e yazan, üzerine **ileriye dönük getiri ölçümü** ve
**şirket-bazlı forensic rapor** üreten bir veri-mühendisliği projesidir.
ABD'deki SEC Form 4 takipçilerinin (ör. OpenInsider) Türk sermaye
piyasaları için **referans implementasyonu** olarak tasarlandı: şeffaf,
yeniden üretilebilir, audit-trail'li, açık kaynak.

## Why this matters

KAP - operated by **Merkezi Kayıt Kuruluşu (MKK)** under Türkiye's capital
markets framework - exposes the entire insider-disclosure feed publicly,
yet there is no open analytical layer comparable to U.S. SEC Form 4
trackers. TrailingEdge fills that gap: a transparent, audit-logged,
reproducible pipeline that any regulator, researcher, or market
participant can stand up locally in under thirty minutes.

The project also serves as a working reference for several
non-trivial integration problems:

- KAP's undocumented Java object-serialization wrapper around PDF downloads
- Turkish-locale numeric / date / encoding handling in `pdfminer`
- Idempotent disclosure ingest under KAP's **silent** 2,000-record result cap -
  the endpoint truncates to the newest rows and drops the older head of the range
  with no error and no cursor, so any window that returns *at* the cap is presumed
  incomplete and bisected until it fits
- Forensic graph analytics over board-interlock data via NetworkX

## Quick start

```bash
cp .env.example .env          # set DATABASE_URL and KAP_BASE_URL
docker-compose up -d          # postgres
uv sync                       # python deps
alembic upgrade head          # schema
```

The CLI installs as `trailingedge` (short alias: `te`). To install straight
from source: `pip install git+https://github.com/caganco/trailingedge`.

Daily ingest:

```bash
trailingedge scrape kap-insider --last-hours 24      # last day
trailingedge scrape kap-insider --last-hours 168     # last week
trailingedge scrape kap-insider --since 2026-05-01 --until 2026-05-27
```

Forensic brief for a single ticker:

```bash
trailingedge report forensic KAPLM
```

## Sample output - daily signal

`reports/sample/daily_signal.example.json` (committed sample, names/ticker
anonymized - live runs write real KAP names to git-ignored `reports/`):

```json
{
  "as_of_date": "2026-05-28",
  "clusters": [
    {
      "ticker": "XXXXX",
      "cluster_score": 42.83,
      "insider_count": 2,
      "window_start": "2026-05-21",
      "window_end": "2026-05-21",
      "unique_insiders": ["INSIDER A", "INSIDER B"],
      "total_buy_value_try": 711360.0
    }
  ],
  "base_rates": {
    "20": {
      "benchmark_ticker": "XU100",
      "signals_with_outcome": 0,
      "verdict": "INSUFFICIENT_POWER",
      "required_n_for_power": 784,
      "hit_rate_pct": 0.0,
      "hit_rate_ci_95": [0.0, 0.0],
      "mean_abnormal_return_pct": 0.0,
      "p_value": 1.0
    }
  }
}
```

Returns are **market-adjusted** against XU100 over the position's own held interval,
and entry is t+1 after the disclosure is public. Every estimate carries a Wilson
interval, a t-test, and a `verdict` - and `INSUFFICIENT_POWER` is a gate, not a
footnote: below ~784 events the point estimates are not evidence in either direction,
so the report declines to offer one. See [`reports/sample/README.md`](reports/sample/README.md)
for what the previous (raw-return, N=29) version of this file claimed and why it was void.

## Technical highlights

| Concern | Implementation |
|---|---|
| HTTP | `httpx` async + `aiolimiter` (2 RPS cap) + `tenacity` retry on 429/503/timeout |
| PDF unwrap | Java `byte[]` serialization stripped at offset 23 (4-byte BE length prefix) |
| PDF parse | `pdfminer.six` with Windows-1252 awareness; date-anchored row extraction |
| Number parse | `1.234,56` → `Decimal("1234.56")` with explicit sign handling |
| Idempotency | `SHA-256(name\|date\|type\|count\|price)` natural key + `ON CONFLICT DO NOTHING` |
| Audit | `scraper_runs` table with `RUNNING → SUCCESS/FAILED/PARTIAL` state machine |
| Schema | SQLAlchemy 2.0 typed `Mapped[...]` ORM + Alembic migrations |
| Result cap | KAP truncates to 2,000 newest rows and silently drops the older head; windows returning *at* the cap are bisected recursively until complete |
| Names | `rapidfuzz token_sort_ratio` + Turkish ASCII transliteration for cross-source joins |
| Graph | NetworkX over `board_interlocks` materialised view with `REFRESH CONCURRENTLY` |
| OCR (optional) | PyMuPDF render @ 300 DPI → Tesseract `-l tur` for Ticaret Sicil gazettes |

## Architecture

```
KAP API
  └─ POST /tr/api/disclosure/members/byCriteria  ──► list (filter DKB)
  └─ GET  /tr/api/notification/attachment-detail ──► detail + objId
  └─ GET  /tr/api/file/download/{objId}          ──► PDF (Java-wrapped)
                                                     │
                                                     ▼
                              parse_dkb_transactions (pdfminer)
                                                     │
                                                     ▼
                                            KapRepository
                                       (upsert disclosure + txs)
                                                     │
                                                     ▼
                                              PostgreSQL
                                                     │
              ┌──────────────────────┬───────────────┴──────────────────┐
              ▼                      ▼                                  ▼
       cluster detection      forward returns                 forensic brief
       (≥N insiders, Δt)      (5/20/60-day horizons)          (HTML + PDF)
```

Detailed data flow, design decisions, and Turkish-locale edge cases:
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md),
[`docs/DATA_DICTIONARY.md`](docs/DATA_DICTIONARY.md),
[`docs/KAP_ENDPOINT_NOTES.md`](docs/KAP_ENDPOINT_NOTES.md).

## Project structure

```
src/trailing_edge/
  core/        config, logging, async http client, db, tz helpers
  scrapers/
    kap/       KAP HTTP client + DKB/ODA parser + orchestrator
    ticaret_sicil/   CAPTCHA-gated TSG client + OCR pipeline (optional)
  models/      SQLAlchemy ORM (disclosures, transactions, graph, signal)
  storage/     repository / upsert layer
  signals/     cluster detection, forward returns, base rates, graph
  reports/     daily signal, forensic brief, network, cross-reference
  data/        yfinance price ingest
  cli/         click entrypoint (trailingedge ...)

docs/          architecture, data dictionary, KAP endpoint reconnaissance
scripts/       backfill, fixture acquisition, ops scripts
migrations/    alembic
tests/         unit/ (no DB) and integration/ (TEST_DATABASE_URL)
```

## Testing

```bash
uv run pytest tests/unit/ -v          # no DB required
uv run pytest tests/integration/ -v   # requires TEST_DATABASE_URL
```

## Known limitations

These are documented openly so consumers can judge the analytics layer
honestly:

- **Disclosure timing.** `transaction_date` in the PDF can pre-date the
  `published_at` of the KAP filing by several days. The current
  cluster-return measurement uses `window_end` (= last transaction date)
  as the entry-price anchor. Strict point-in-time backtesting should
  instead use `max(transaction_date, published_at)` to avoid leaking the
  filing date forward - tracked for the next analytics revision.
- **Sample size.** Forward-return base rates are computed over a
  small (~30-signal) live window. A historical backfill is required
  before the numbers can be treated as anything stronger than
  *indicative*.
- **Excess returns.** Returns are absolute, not benchmarked against the
  XU100 index. An excess-over-benchmark view is straightforward to add
  but out of scope for the current phase.
- **ODA disclosures.** Fund-company threshold-crossing reports (Article 12)
  are stored at the disclosure level but not yet parsed into the
  transaction table.
- **TSG OCR.** The Türkiye Ticaret Sicil Gazetesi pipeline is
  CAPTCHA-gated (semi-automatic) and intended for forensic enrichment,
  not for any market-signal claim.

## Status

Phase 1 - working end-to-end pipeline (ingest + analytics + briefs) on
a single-node deployment. Production hardening (HA Postgres, scheduled
ingest, alerting) is out of scope for this revision.

## License

[MIT](LICENSE)

---

**Not affiliated.** TrailingEdge is an independent, non-commercial, open-source
project. It has no connection to, and no sponsorship or endorsement from, Borsa
İstanbul A.Ş., the Public Disclosure Platform (KAP), Merkezi Kayıt Kuruluşu
(MKK), or the Capital Markets Board of Türkiye (SPK). "Borsa İstanbul", "BIST",
"KAP", "MKK" and "SPK" are names or marks of their respective owners, used here
only descriptively to identify the public market and the official disclosure
sources this project analyses. All data is derived from publicly available
regulatory disclosures.

**Not investment advice.** This is a research and data-engineering tool. Nothing
it produces is investment advice, a recommendation, or a solicitation to buy or
sell any security. Forward-return figures are empirical base rates over small
samples and carry no predictive guarantee. Use at your own risk.
