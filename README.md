# TrailingEdge

> **Do BIST insiders' disclosed purchases predict returns you can actually capture?**
>
> They predict. You cannot capture them. This is the pipeline that measures both,
> and the second half is the finding.

[![Python](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org)
[![PostgreSQL](https://img.shields.io/badge/postgres-16-336791.svg)](https://www.postgresql.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![CI](https://github.com/caganco/trailingedge/actions/workflows/ci.yml/badge.svg)](https://github.com/caganco/trailingedge/actions/workflows/ci.yml)
[![mypy](https://img.shields.io/badge/mypy-checked-2a6db2.svg)](https://mypy-lang.org/)
[![Ruff](https://img.shields.io/badge/ruff-checked-d7ff64.svg)](https://github.com/astral-sh/ruff)

---

## The result

Insider-cluster events on Borsa İstanbul, 2015-2018. Entry is t+1 after the KAP
disclosure is *public*, returns are measured in excess of XU100 over the same held
interval, and the round-trip cost is estimated per trade from that stock's own OHLC
(Abdi-Ranaldo 2017) rather than assumed as a flat fee.

| Horizon | N | Gross AR | Cost | **Net AR** | t (net) |
|---|---:|---:|---:|---:|---:|
| 5d | 1,032 | +0.57% | 3.34% | **−2.77%** | −13.11 |
| 20d | 1,032 | +1.76% | 3.34% | **−1.58%** | −4.04 |
| 60d | 1,032 | +2.09% | 3.34% | **−1.24%** | −1.98 |

**The signal is real. The gross abnormal return is significantly positive at every
horizon** (20d: +1.76%, t = 5.12). **And it is not tradeable**, because insider clusters
fire in illiquid small caps whose bid-ask spread is wider than the alpha: the median
round trip costs 1.94%, the upper quartile 4.24%. Nothing survives crossing it twice.

That is the whole finding, and it is why this repository exists. A gross number is not
an edge; an edge is what is left after the market takes its cut.

Everything below is the machinery required to be able to say that honestly - and the
audit trail of the six silent data faults that had to be found first, each of which had
moved the number:

| | |
|---|---|
| KAP's list endpoint truncates at 2,000 rows, keeping the newest | ~75% of every month was being discarded |
| The transaction-date regex accepted `/` but not `.` | every filing before 2021 parsed to **zero** transactions |
| Fixed column indices into a variable-width table | 21% of stored rows were silently wrong |
| WAF disconnects were caught and skipped | 12% of disclosures vanished, run still reported `SUCCESS` |
| Prices fetched in one batch; one bad symbol poisoned the rest | looked exactly like survivorship bias |
| yfinance serves nothing for a delisted ticker | 31% of clusters dropped - the dead ones, the worst outcomes |

The last of those was fixed by loading Borsa İstanbul's own end-of-day bulletin, which is
survivorship-clean by construction: 1.3M rows, 747 tickers, against yfinance's 185. It
also carries the VBTS tradability flags - which, once measured, turned out to touch only
1% of entries and not to be the constraint at all.

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

- **Prices trades against the exchange's own bulletin**, not a retail feed: survivorship-
  clean by construction, corporate-action-adjusted by chaining the exchange's restated
  previous close, and carrying the VBTS gross-settlement and suspension flags.
- **Refuses to answer when it cannot.** `compute_base_rate` returns
  `INSUFFICIENT_POWER` below ~784 events and `SURVIVORSHIP_BIASED` when too many
  clusters cannot be priced. Both gates fired during this work, and both were right.

> **What is claimed, precisely:** a statistically strong *gross* abnormal return
> (20d: +1.76%, t = 5.12, N = 1,032, survivorship-clean) that does **not** survive a
> per-trade cost estimate. The window is 2015-2018 - a single regime - so the result is
> not yet regime-conditional, and that is stated rather than glossed. Remaining gaps are
> in [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md#6-still-open), not left for a
> reader to discover.

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

Insider-activity brief for a single ticker (HTML + PDF):

```bash
trailingedge report generate --ticker SARKY
```

## Reproducing the result

```bash
trailingedge prices backfill                    # XU100 benchmark (yfinance)
python scripts/load_official_prices.py          # exchange bulletin: survivorship-clean
trailingedge signal detect                      # clusters + market-adjusted outcomes
python scripts/check_forward_returns.py         # gross abnormal return, with its gates
python scripts/net_of_cost.py                   # the one that decides it
```

`net_of_cost.py` is the script that answers the question:

```
=== Abnormal return, NET of round-trip cost (order 25,000 TRY) ===
    spread: Abdi-Ranaldo (2017) from the stock's own OHLC, per trade
    round-trip cost: median 1.94%  p25 1.22%  p75 4.24%

HORIZON     N  GROSS AR%   COST%  NET AR%   HIT%          95% CI      t  VERDICT
     5d  1032       0.57    3.34    -2.77   26.6    [23.9, 29.3] -13.11  LOSES MONEY (net)
    20d  1032       1.76    3.34    -1.58   41.4    [38.4, 44.4]  -4.04  LOSES MONEY (net)
    60d  1032       2.09    3.34    -1.24   44.9    [41.9, 47.9]  -1.98  LOSES MONEY (net)
```

The spread is not a parameter. It is estimated for each trade from the 30 sessions of
that stock's own OHLC before entry - which is also the only estimator that works on the
delisted names the bulletin carries and no quote feed does. A flat fee would have made
the answer come out the other way.

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
