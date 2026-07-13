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

## 5. What was open, and how it closed

Each of these was listed here as a known gap while it was one. They are kept, with what
measuring them actually showed - a limitation that is named and then measured away is
worth as much as one that turns out to be fatal. What is worth nothing is leaving it
unmeasured and implied.

### Transaction cost - CLOSED, and it is the binding constraint

The gross abnormal return was always an upper bound, and this is what it was an upper
bound over.

The spread is estimated **per trade** from the 30 sessions of that stock's own OHLC
before entry (Abdi & Ranaldo 2017, RFS 30(12)) - not assumed as a flat fee, which would
have flattered the answer, and not taken from a quote feed, which does not exist for the
delisted names the exchange bulletin carries. Impact is Kyle/Almgren square-root on the
same window; commission and BSMV are charged per side.

    round-trip cost:  median 1.93%   p25 1.19%   p75 4.34%

    horizon   N=1070   gross AR   net AR      t (net)
    5d                  +0.66%    -2.71%     -12.91
    20d                 +2.02%    -1.35%      -3.31
    60d                 +2.18%    -1.20%      -1.86   (not significant)

**The signal does not survive the cost of trading it.** Insider clusters fire in illiquid
small caps, and the spread on those names is wider than the alpha. This is the project's
result, not a caveat on it. At 60 days the net loss stops being statistically
distinguishable from zero, which is not a reprieve: the point estimate is still negative,
and a signal that *may* break even over three months is not an edge either.

A second trap, found later and more serious than the first, because it sat under the
number that decides the answer. `price_history.close_try` is a **chained total-return
index**, not a price - correct for returns, and every return here is computed from it. But
the tick floor is 0.01 TRY on the exchange's grid, and ADV is price x volume, and both
were being handed the index. BIST companies issue bonus shares constantly, so the index
and the print pull apart: measured on 2018-12 bulletin data, a median 0.98x but a range of
0.60x to 118x, with 32% of ticker-days off by more than 10%. Because the factor falls on
both sides of 1, the error was not conservative - it mispriced trades in both directions,
worst in the serial bonus-issuers, which are small caps, which is exactly where the tick
floor binds. Migration 0008 keeps the raw print alongside the index; the spread estimator
is scale-free and correctly stays on the index, while the floor and ADV moved to the
price. Correcting it *raised* N from 1,032 to 1,070 (the earliest 2015 clusters had been
silently dropped for want of a 22-session lookback, and those clusters averaged +8.45% at
20 days against +1.93% for the rest) and left the verdict standing.

One trap worth recording: the first version of the cost script *dropped* any trade whose
Abdi-Ranaldo window came back degenerate (gamma >= 0, so a zero spread). That discarded
96% of the sample - and the survivors' gross abnormal return came out **negative** where
the full sample's was positive. The exclusion selects on exactly the price behaviour the
signal is about. A quiet window is not a free trade: the estimate now widens the window
and is floored at one tick, and nothing is dropped for it.

### VBTS tradability - CLOSED, and it is NOT the constraint

Borsa İstanbul escalates measures on volatile names: short-selling ban → **gross
settlement** → single-price auction. A name under gross settlement cannot be round-tripped
the way a backtest assumes. Insider clusters fire in exactly the names this happens to, so
this looked like it might matter a great deal.

The exchange bulletin carries the flags (`BRUT TAKAS`, `GECICI DURDURMA`), so they are now
loaded. Measured: 143,012 gross-settlement ticker-days across 451 names - 11% of all price
rows. But of 1,079 cluster entries, **only 16 (1.5%)** land on a restricted day.

So the gap is closed and it was never the binding constraint. The cost is.

### Routine vs opportunistic - CLOSED, and the split does not exist here

Pre-registered in `docs/stage0/OPPORTUNISTIC_CLASSIFIER.md` and frozen before it was run,
because this was the signal's last plausible route to being tradeable and therefore
exactly where a definition chosen after the fact would be most tempting.

    cluster classes at 20d:  OPPORTUNISTIC=1066   ROUTINE=0   UNCLASSIFIED=13

    OPPORTUNISTIC  20d  N=1059   gross +2.08%   cost 3.38%   net -1.31%   t = -3.18
                                                               LOSES MONEY (net)

**Not one cluster classified as routine**, and the frozen document had said why in advance:
SPK II-15.1's 250,000 TRY reporting threshold already censors the small, quiet, scheduled
trades that CMP's routine class is built from. Turkish insiders have no routine *filings*
because routine trades are never disclosed at all.

So there is no noise to strip. The opportunistic subset IS the sample (1,066 of 1,079), its
gross return is +2.08% against the pooled +2.07% - indistinguishable - and it loses to the
same spread. The pre-registered prior said the subset "will be stronger gross but will
still not clear the spread"; it was not even stronger.

CMP's hypothesis is not refuted - it is **inapplicable**. The regulation that makes this
dataset possible is the same regulation that removes the variation the test needs. That is
a finding about Turkish disclosure, not about insiders.

*Limitation, stated:* the 36-month lookback is thin on 3.5 years of data, so a fuller
backfill would classify more insiders and might surface some routine ones. The direction is
known - stripping routine trades RAISES gross alpha - but it would have to raise +2.08%
past the 3.38% mean cost, which is far more than CMP's own effect size, and the disclosure
threshold has already removed most of what would do the raising.

### Split-sample stability - the conclusion holds in both halves

Not a regime test - see §6 - but a check that the result is not carried by one stretch of
the sample. The same cost test, run separately on each half of the available window:

    period        N     gross%   cost%    net%       t     verdict
    2015-2016    859     +2.30    3.57   -1.27    -3.04    loses money
    2017-2018    197     +0.30    2.17   -1.87    -1.57    inconclusive (N < 200)

**Net abnormal return is negative in both halves.** The second is inconclusive rather than
confirming, but only because N = 197 falls under the pre-registered minimum of 200 - the
sign and the direction agree; the power does not.

Worth recording without over-reading: the *gross* alpha collapses from +2.30% to +0.30%
between the halves. That could be the market becoming more efficient, or it could be
sampling noise at N = 197. It is not interpreted here, because at that N it cannot be.

## 6. Still open

**Regime.** The window is 2015-2018. That spans the August 2018 currency crisis but not
the 2021-2023 negative-real-rate retail boom or the 2023+ normalisation. The result is
therefore **not regime-conditional**, and a signal that dies to the spread in one regime
could in principle survive in another where those names traded tighter. The honest position
is that this is untested, not that it is unaffected.

Closing it needs the KAP backfill to reach 2026. That is a data-collection problem, not a
methodological one, and it does not touch the mechanism: the spread eating the alpha is a
microstructure fact about illiquid names, not a regime phenomenon.

**Cluster scoring is close to single-factor.** `cluster_score` blends insider count (0.50),
role seniority (0.30) and recency (0.20). In historical mode recency is pinned at 1.0, so
20% of the weight is a constant, and seniority falls back to its 0.5 default wherever the
scraped board roster does not cover an insider. When coverage is zero the score reduces to
a monotone function of `insider_count` alone - `detect_clusters` now logs `role_map_empty`
loudly in that case, where it used to happen silently. The score is not used to gate any
result reported here, so this is a latent defect rather than an active one.

**Kyle's lambda is uncalibrated** (1.0). At retail order size the impact term is small
enough that the error changes no conclusion; at institutional size it would, and the number
should not be trusted there.

## References

- Kothari, S.P. & Warner, J.B. (2007). *Econometrics of Event Studies.* Handbook of
  Empirical Corporate Finance.
- Cohen, L., Malloy, C. & Pomorski, L. (2012). *Decoding Inside Information.* Journal
  of Finance 67(3), 1009–1043.
- SPK, *Özel Durumlar Tebliği (II-15.1)*, Art. 11 — disclosure obligation and the
  250,000 TRY annual threshold.
- Borsa İstanbul, *Volatilite Bazlı Tedbir Sistemi (VBTS)*.
