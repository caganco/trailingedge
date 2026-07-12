# Methodology

Every choice below is a pre-registered methodology decision, not a tuning knob.
Changing one changes what a result *means*, so each is recorded here with its
reason and its source.

---

## 1. When can the signal be acted on?

**Entry is t+1 after the KAP disclosure is public — never the transaction date.**

An insider's trade is private information until the filing appears. Entering on
`transaction_date` prices a bar nobody could have traded at, and books the entire
filing lag as free return.

The lag is not one or two days. Under **SPK II-15.1 Art. 11**, a person with
administrative responsibility owes *no disclosure at all* until their transactions
cross a cumulative **250,000 TRY** threshold within the calendar year. Trades can
therefore accumulate privately for months and surface in one filing once the
threshold is crossed. Any backtest keyed to the transaction date silently treats
that entire window as knowable.

Implementation: `signals/entry_timing.py` takes the **latest** `published_at` among
the disclosures backing a cluster — correction-aware, since a corrected filing is
only fully public when the correction itself is filed — and enters at the first
trading day strictly after it. A cluster with no resolvable public disclosure is
**skipped**, never quietly fallen back to the transaction date.

> **Data-coverage limit, stated rather than hidden.** The 250,000 TRY threshold means
> the KAP dataset is *structurally* blind to small insider accumulation. Quiet buying
> that stays under the threshold is invisible by regulatory design. This caps what any
> model built on this source can see, and no amount of modelling recovers it.

---

## 2. What counts as a return?

**The primary metric is the market-adjusted abnormal return:**

```
AR = R_stock − R_benchmark      (same held interval)
```

A raw forward return is not evidence. BIST quotes nominal TRY returns under high
inflation, so the unconditional drift of a randomly chosen stock over 20–60 trading
days is materially positive. Scoring a hit rate against a 50% coin-flip prior, or a
mean against 0%, credits market drift and beta to the signal.

Both legs are priced over the position's **own** entry and exit dates, not at the
benchmark's own trading-day offsets. Those calendars diverge whenever the stock is
halted — a BIST single-price or VBTS measure — and that is precisely the population
insider clusters concentrate in.

Implementation: `signals/abnormal.py`, `signals/returns.py`.

### Why market-adjusted rather than a market model?

The market-adjusted model fixes α=0, β=1 instead of estimating them over a
pre-event window. This is a deliberate call, not a shortcut:

- **Kothari & Warner (2007), _Econometrics of Event Studies_:** for short-horizon
  event studies, test-statistic specification is *not* highly sensitive to the
  benchmark model of normal returns. Estimating β buys little and costs an
  estimation window.
- On thinly traded BIST small caps, that estimation window is exactly where β is
  least reliable, and non-synchronous trading biases OLS β downward.

The 60-day horizon sits on the boundary of "short". Kothari & Warner note long-horizon
tests have low power and *are* benchmark-sensitive — so the 60-day figure is weaker
evidence than the 5- and 20-day figures, not stronger.

### Why XU100?

A review of 75 Borsa İstanbul event studies finds **BIST-100 with market-adjusted
returns** to be the field's standard market proxy. Using the house benchmark keeps
results comparable to the published literature rather than to a private convention.

**Known limitation.** XU100 is a large-cap index; insider clusters concentrate in
smaller names, so the size premium is not fully controlled. A size-matched benchmark
portfolio is the documented next step. XU100 is the literature default and the honest
first cut — it is an assumption on the record, not a silent one.

---

## 3. When is a result allowed to be called a result?

**Never below the power gate.**

Separating a 55% hit rate from a 50% null at α=0.05 with 80% power requires

```
n = (z_{α/2} + z_β)² · p(1−p) / (p₁ − p₀)²
  = (1.96 + 0.84)² × 0.25 / 0.05²
  ≈ 784 observations
```

`compute_base_rate` returns a **`verdict`** field, and `INSUFFICIENT_POWER` is a gate,
not a footnote. Below the threshold the point estimates are not evidence in *either*
direction, and downstream reports must branch on `verdict` rather than reading a hit
rate off the number.

Every estimate additionally ships with:

- a **Wilson score interval** on the hit rate — preferred over the normal (Wald)
  interval, which misbehaves at small n and near 0/1, exactly the regime this project
  keeps landing in;
- a **two-sided t-test** of mean AR against a zero null.

The p-value uses a normal approximation to the t distribution (`math.erfc`), accurate
at the sample sizes the gate admits.

Implementation: `signals/base_rate.py`.

---

## 4. Is the dataset even complete?

**It was not, and the gap was invisible.**

KAP's disclosure-list endpoint (`/tr/api/disclosure/members/byCriteria`) returns at most
**2,000 rows**. On overflow it keeps the **newest** rows and silently drops the older head
of the requested range — no error, no pagination cursor, no flag of any kind. Measured
2026-07:

| Requested window | Rows returned | Dates actually covered |
|---|---:|---|
| 2024-03-01 → 2024-03-31 | 2,000 | **25–31 March only** |
| 2024-01-01 → 2024-12-31 | 2,000 | **21–31 December only** |
| 2024-03-01 → 2024-03-07 | 1,758 | complete (under cap) |

The backfill chunked by **month**. Every monthly chunk therefore came back at the cap, and
the ingester saw roughly **the last week of each month and discarded the other ~75%** —
from the project's first run onward. The N=29 sample was never a statement about how much
insider activity exists; it was an artefact of a silent truncation.

There is no server-side subject filter to lean on: `subjectList` expects member OIDs rather
than the display string, and a `disclosureClass` key is not honoured — both return zero rows.
So the range itself has to be narrowed. `KapClient._fetch_list_complete` now treats any
response that comes back **at** the cap as presumed incomplete, bisects the date range, and
recurses until every window fits, merging on `disclosureIndex`. A single day denser than the
cap cannot be narrowed further by date and is logged as `kap_list_day_over_cap` rather than
returned as if it were whole.

**Reachable sample size.** True volume measured on uncapped (weekly) windows is **~22 DKB
disclosures/week ≈ 1,100/year**, so 2015–2026 holds on the order of **12,000** insider
disclosures. The ~784-event power gate is therefore reachable — which it never was under
the truncated ingest. `backfill_kap_insider.py` defaults to `2015-01-01` for exactly this
reason.

---

## 5. Known open issues

These are real and unfixed. They are listed so a reader does not have to discover
them by reading the source.

**Cluster scoring is close to single-factor.** `cluster_score` blends insider count
(0.50), role seniority (0.30) and recency (0.20). In historical mode recency is pinned
at 1.0, so 20% of the weight is a constant. Seniority is resolved by joining the KAP
board/executive roster (`signals/roles.py`) — but where the roster does not cover an
insider, seniority falls back to its 0.5 default. When coverage is 0 the score reduces
to a monotone function of `insider_count` alone. `detect_clusters` now logs
`role_map_empty` loudly in that case; it used to happen silently.

**No routine/opportunistic split.** Cohen, Malloy & Pomorski (2012), *Decoding Inside
Information* (JF 67(3)) show that **over half** of insider trades are "routine" —
predictable, compensation- or liquidity-driven — with **essentially zero** abnormal
return, while the remaining "opportunistic" trades carry ~82bp/month. This pipeline
does not yet separate them, so it averages the informative trades against the
uninformative ones. Porting their classifier (an insider is *routine* if they traded in
the same calendar month for three consecutive years) requires per-insider histories the
250,000 TRY threshold makes sparse — a Türkiye-adapted definition has to be
pre-registered before it is measured, not fitted afterwards.

**No tradability filter.** Insider clusters concentrate in illiquid names, and Borsa
İstanbul's VBTS applies escalating measures to exactly those: short-selling ban →
**gross settlement** → **single-price auction**, in 15-day steps. A stock under a
single-price measure cannot be entered at the close the way the backtest assumes.
Neither VBTS state nor a liquidity floor is currently applied to the universe, and no
spread or market-impact cost is deducted. **Any positive result from this pipeline is
therefore an upper bound, before frictions.**

---

## References

- Kothari, S.P. & Warner, J.B. (2007). *Econometrics of Event Studies.* Handbook of
  Empirical Corporate Finance.
- Cohen, L., Malloy, C. & Pomorski, L. (2012). *Decoding Inside Information.* Journal
  of Finance 67(3), 1009–1043.
- SPK, *Özel Durumlar Tebliği (II-15.1)*, Art. 11 — disclosure obligation and the
  250,000 TRY annual threshold.
- Borsa İstanbul, *Volatilite Bazlı Tedbir Sistemi (VBTS)*.
