# Stage-0: routine vs opportunistic insiders

**Frozen 2026-07-13, before any result was computed.**

This file exists so the definition cannot be chosen after seeing which one works. Every
parameter below is fixed here; the measurement code reads them and must not introduce
others. If a definition needs to change, it changes *here*, with a note saying why, and
the previous result is reported alongside the new one - never replaced by it.

## Hypothesis

Cohen, Malloy & Pomorski (2012), *Decoding Inside Information*, JF 67(3): more than half
of insider trades are **routine** - predictable, compensation- or liquidity-driven - and
carry **essentially zero** abnormal return. Stripping them leaves an **opportunistic**
subset worth ~82bp/month value-weighted.

TrailingEdge currently measures every cluster the same way. Its gross abnormal return is
+1.76% at 20 days against a median round-trip cost of 1.94% - it loses by a hair. If the
CMP result carries to BIST, the opportunistic subset should be materially stronger, and
the question is whether it is stronger *enough to clear the spread*.

This is the signal's last plausible route to being tradeable. It is therefore exactly the
place where a definition chosen after the fact would be most tempting, and least honest.

## Definition (frozen)

CMP classify an insider as routine if they traded **in the same calendar month for three
consecutive years**, and require three years of history to classify at all.

That test cannot be ported as-is. Under SPK II-15.1 Art. 11 an insider owes no disclosure
until their transactions cross a cumulative **250,000 TRY** threshold within the calendar
year, so most Turkish insiders have sparse, gappy filing histories. Requiring three
consecutive years of filings would classify almost nobody and would select on filing
frequency - which correlates with position size, which correlates with the outcome.

So the adaptation, fixed now:

An insider is **ROUTINE** at time *t* if, over the 36 months before *t*, they have filed
purchases in **at least 3 distinct calendar months**, and **≥60%** of those purchase
months fall in the **same calendar month of the year** (e.g. every March).

An insider is **OPPORTUNISTIC** at *t* if they have at least one prior purchase in the
36-month window and are not ROUTINE.

An insider with **no prior purchase** in the window is **UNCLASSIFIED**. They are reported
separately and are NOT merged into either group: CMP's own result rests on a trading
history, and an insider without one is not evidence for or against it.

A **cluster** is opportunistic if **at least one** of its constituent insiders is
opportunistic at that cluster's signal date. (A cluster is a co-purchase event; one
informed participant is enough to make it informative. The alternative - requiring all
insiders to be opportunistic - is a stricter test and is NOT run, to avoid a second
specification to choose between.)

All classification uses only filings whose `published_at` is at or before the cluster's
signal date. No look-ahead.

## Primary test (frozen)

- **Metric**: mean abnormal return, net of the per-trade round-trip cost already used in
  `scripts/net_of_cost.py` (Abdi-Ranaldo spread + Kyle impact + commission/BSMV).
- **Horizon**: **20 trading days**. Chosen because it is where the gross signal is
  strongest in the pooled result and because CMP's effect is monthly. It is the single
  pre-specified horizon; 5d and 60d are reported but are *secondary*.
- **Null**: net abnormal return of the opportunistic subset ≤ 0.
- **Decision**: the subset is declared tradeable only if its mean net abnormal return is
  positive with a two-sided t-test at **p < 0.05**, on **N ≥ 200** opportunistic clusters.
- Below N = 200, the verdict is INSUFFICIENT_POWER and no claim is made either way.

## What would falsify the hypothesis

Opportunistic clusters showing no materially higher gross abnormal return than routine
ones. That would say the CMP split does not carry to BIST insider disclosures - which is
a publishable result and the expected one, given the 250,000 TRY threshold already
censors exactly the small, quiet, routine trades CMP's routine class is built from.

## Pre-registered expectation

Honest prior, recorded before running: **the opportunistic subset will be stronger gross
but will still not clear the spread.** The gap is 1.76% vs 1.94% pooled; a 2x improvement
in gross alpha would clear it, but CMP's own effect size (82bp/month) is not 2x a 1.76%
20-day return - it is comparable to it. And BIST's disclosure threshold has already
removed much of what CMP calls routine, so there is less dilution left to strip out than
in the US sample.

Recording this matters: if the result comes out positive, it comes out *against* the
prior, and that is worth more than a confirmation.

---

## Addendum, 2026-07-13 (after the fact - the text above is unchanged)

Nothing above this line has been edited. A pre-registration that gets rewritten once the
answer is known is worth less than no pre-registration at all, so the baseline figures in
it (+1.76% gross against a 1.94% median cost) stay exactly as they were written, even
though both numbers have since moved.

They moved because a fault was found in the cost model *after* this document was frozen:
`price_history.close_try` is a chained total-return index, and the tick floor and ADV were
reading it as if it were a traded price. On the corrected basis the pooled figures are
+2.07% gross (N = 1,079) against a 1.93% median / 3.37% mean round-trip cost. See
`docs/METHODOLOGY.md` §5.

**The frozen decision rule was applied to the corrected data, not re-chosen for it:**

    OPPORTUNISTIC  20d (primary)  N = 1,059   gross +2.08%   net -1.31%   t = -3.18
    ROUTINE                       N = 0

- N = 1,059 clears the pre-registered MIN_N of 200.
- Net is negative with p < 0.05, so the subset is **not** declared tradeable.
- The recorded prior said the opportunistic subset "will be stronger gross but will still
  not clear the spread." Half right, and the wrong half is the interesting one: it was
  **not stronger gross at all** (+2.08% against the pooled +2.07%). There was no routine
  class to strip, because the 250,000 TRY disclosure threshold means routine trades are
  never filed in the first place.

The hypothesis is not refuted. It is inapplicable.
