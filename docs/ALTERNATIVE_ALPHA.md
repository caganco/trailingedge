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

The **mean** looked spectacular — and was a trap:

    regime     institutional hub, MEAN net   MEDIAN net   liquid* MEDIAN net
    2015-2018  +22.3%  t=4.40                 -0.7%        -2.7%  (t=1.33, n.s.)
    2019-2020   +5.3%  t=3.10                 +0.3%        -2.5%
    2021-2026   -7.3%  t=-2.59                -4.3%        -4.7%
    *liquid = ADV >= 1,000,000 TRY, i.e. names you could actually trade

**This is a self-caught false positive, and worth keeping as the cautionary tale it is.** The
+22.3% mean in 2015-2018 is driven almost entirely by a handful of extreme outliers on one
near-untradeable instrument: ISKUR (İş Bankası founder shares) prints returns of +873% but
trades **~32,000 TRY a day** — a 25,000 TRY order is most of a day's volume, so the return is
uncapturable in principle. Strip the top 10 events and the hub mean falls from 13.8% to 6.2%;
the whole result rests on 17 actors and 41 tickers. The **median** hub trade **loses money
after cost in every regime**, and once the untradeable names are filtered out the mean is
insignificant even in the best period (t=1.33). Point-in-time connectivity and regime splits
were done correctly; the error was trusting the mean on skewed, illiquid data — the exact trap
this project exists to avoid, committed by the person cataloguing it.

The one robust piece is the mirror image: a coordinated **pack** — three or more different
insiders piling into one name within 20 days — **underperforms**, sharply in the recent regime
(net −10%, t=−6.7). Many insiders buying at once is late-stage crowd, and it reads as a
contrarian tell, not a pack of wolves to follow.

## Where this leaves it

There is **no exception**. Every candidate here — insider clusters, holding pairs, bonus and
tender events, insider-buy leakage, named persistence, share classes, and the network signal —
fails once held to the same standard: non-overlapping / out-of-sample where relevant, cost per
trade, the **median** rather than the outlier-inflated mean, and a **liquidity filter** that
removes names you cannot actually trade. Retail-accessible, public-signal alpha on BIST
equities is, on this survivorship-clean 2015-2026 evidence, structurally absent.

The alpha itself is real and it is **pre-announcement**: the tender-win run-up at t=7.86 is
direct, quantitative evidence that someone positions ahead of the public disclosure. Public
data can *detect* that (the run-up, the sparse volume footprint) but cannot *time* or capture
it — and the disclosed-insider record does not reveal who does. Capturing it would require
information or infrastructure that is either non-public (illegal) or unavailable to a retail
account. That is the honest, defensible finding, and it is worth more than any of the false
edges that did not survive contact with the median.
