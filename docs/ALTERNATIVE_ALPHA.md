# Alternative alpha on Borsa İstanbul — a rigorous search, and where it lands

The core project shows that disclosed-insider *clusters* predict a real gross abnormal return
that does not survive the spread, and that the signal decayed after 2020 (see
[`METHODOLOGY.md`](METHODOLOGY.md)). This document is the log of a wider search for a
capturable edge on the same survivorship-clean 2015-2026 data — what was tried, what it
showed, and the one thing that actually worked before it too decayed.

The discipline is the same throughout: entry at t+1 after **public** information, abnormal
return in excess of XU100, per-trade round-trip cost (Abdi-Ranaldo spread + Kyle impact +
commission/BSMV), and — where a candidate looked alive — **out-of-sample / point-in-time**
validation, because the fastest way to fool yourself here is an in-sample t-stat.

## What was tested, and what it showed

| Candidate | Result |
|---|---|
| **Insider clusters (pooled)** | Gross +1.6% @20d (t=5.2), **net −2.6%** — uncapturable; illiquid names, spread wider than alpha |
| **Liquid subset of the cluster signal** | Cost falls with liquidity, but the gross signal falls with it too; top-decile net +1.3% is not significant (t=0.7) |
| **Holding vs listed-subsidiary pairs** | Looked strong in-sample (KCHOL t=13) but **collapsed** on non-overlapping, regime-split re-test (t≈1.9, inconsistent) — the t was overlap inflation |
| **Bonus (bedelsiz) anticipation** | Big run-up into the ex-date (+9.6% mean), but conditioned on the **public board-decision date** the capturable window is nil (median 0%, liquid names negative). The move is pre-announcement |
| **Tender / contract wins** (KAP "İhale Süreci / Sonucu", "Yeni İş İlişkisi") | **Pre-announcement run-up +2.26%, t=7.86** — the leak is real and measurable — but the post-announcement (tradeable) window has a negative median and is zero in liquid names |
| **Insider buys before those announcements** | 9.4% vs 9.2% placebo — **1.02×**, the leak is *not* visible in disclosed insider data |
| **Abnormal volume before announcements** | Median 0.96× baseline — the leak's footprint is **sparse**, present in a minority of events |
| **Insider sells** | Informative: gross **−2.3% (t=−4.0)** — an avoid signal, but shorting these names is restricted and cost-heavy |
| **Named-insider persistence** | Top-quartile insiders of 2015-2020 returned **−12.3% out-of-sample** in 2021-2026 — past performance anti-predicts; pure regression to the mean |
| **Share-class arbitrage** (KRDMA/B/D, ISATR/BTR/CTR …) | Only a handful of true pairs; none significant (Kardemir t=−1.5) |

The recurring shape: the alpha is real and it is **pre-announcement** — it accrues to whoever
is positioned before the public disclosure. Every strategy built on the public signal, traded
after the fact, either dies to the spread or lives only in illiquid names. This is the
"çakallık" made quantitative: the tender-win run-up at t=7.86 is direct evidence that someone
trades ahead of the news, and the leak is invisible in the disclosed insider data and only
sparsely in volume.

## The one thing that worked: connected institutional insiders

If the edge is in *who is positioned early*, the network is the place to look. Splitting
disclosed insider **buys** by the actor's connectivity — how many distinct companies they have
traded, computed **point-in-time** — and by whether the actor is an institution (holding /
fund / bank) rather than an individual (`scripts/network_signal.py`):

    regime     institutional hub (>=3 companies)   everyone else
    2015-2018  net +22.3%   t=4.40                 net -1.1%
    2019-2020  net  +5.3%   t=3.10                 net -15.5%
    2021-2026  net  -7.3%   t=-2.59                net  -5.6%

Connected institutional accumulation — a holding or fund building a position across several
listed companies — was a **real, point-in-time, net-of-cost edge through 2020**, the single
strongest result in the project and a validation of the original thesis: the connected actors
*were* the informed money, and following their disclosed buys paid. It is not overfitting; the
connectivity is measured only from past trades and the return is after cost.

Two honest caveats sit next to it:

- **It decayed.** In 2021-2026 the same signal is net −7.3% (t=−2.59), along with every other
  signal in the project. Whether that is permanent efficiency or a distortion of the 2021-2023
  lira/inflation regime cannot be known without more forward data.
- **The crowd is the opposite of the hub.** A coordinated *pack* — three or more different
  insiders piling into one name within 20 days — **underperforms**, sharply in the recent
  regime (net −10%, t=−6.7). Many insiders buying at once is late-stage crowd, not smart money;
  it is a contrarian tell, not a "pack of wolves" to follow.

## Where this leaves it

Retail-accessible, public-signal alpha on BIST equities is, on this evidence, structurally
thin: the capturable edge is in pre-announcement positioning, which public data can *detect*
(the run-up) but not *time*. The one exception — following connected institutional insiders —
was genuinely capturable historically and is the natural thing to keep instrumented, because
if the edge returns in a new regime, that is where it would show first. The richer network
layer (board interlocks and ownership, via `graph scrape-management`) is the untested
enrichment: connections that exist independently of trading, which the trade-only network here
cannot see.
