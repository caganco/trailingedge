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
