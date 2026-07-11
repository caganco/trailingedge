# Sample daily signal report

`daily_signal.example.json` is a faithful example of what `trailing-edge report daily`
emits — including, right now, its refusal to report anything.

## Why every base rate reads `INSUFFICIENT_POWER` with `signals_with_outcome: 0`

Migration `0006` made the market-adjusted **abnormal return** the primary metric.
Outcomes written before that migration have `abnormal_return_pct = NULL` and are
deliberately **not** backfilled with a guessed benchmark — a NULL that forces a
recompute is safer than a plausible wrong number. `compute_base_rate` counts only
rows it can market-adjust, so until the pipeline is re-run the honest answer is
"nothing measured yet".

To populate it:

```bash
trailing-edge prices backfill      # fetches XU100 alongside the signal universe
trailing-edge signal returns       # recomputes outcomes with abnormal returns
trailing-edge report daily
```

## What the previous version of this file claimed, and why it was void

For the record, since the numbers were public:

| Horizon | N | Hit rate | Mean **raw** return |
|--------:|--:|---------:|--------------------:|
| 5d  | 29 | 44.83% | −2.00% |
| 20d | 29 | 55.17% | −0.48% |
| 60d | 23 | 52.17% | +5.91% |

Two things make those figures unusable as evidence, and both are now fixed in code:

**They were raw returns, not abnormal returns.** BIST quotes nominal TRY under high
inflation, so a randomly chosen stock rises well over half the time and its mean
return is not zero. A "hit rate" scored against a 50% coin-flip prior and a mean
scored against 0% hand the signal credit for market drift and beta. The 60-day
`+5.91%` in particular is comfortably inside what the index alone returned over the
same span. See `src/trailing_edge/signals/abnormal.py`.

**N was 29.** The 95% Wilson interval around a 55.17% hit rate is roughly
**[37%, 72%]** — it contains the 50% null with room to spare. Separating a 55% hit
rate from 50% at α=0.05 with 80% power needs

```
n = (1.96 + 0.84)² × 0.25 / 0.05² ≈ 784
```

observations. At N=29 the sample cannot distinguish this signal from a coin, in
*either* direction. Publishing the point estimate without that interval was the
actual error; the number itself was never the problem.

`compute_base_rate` now returns a `verdict` field, and `INSUFFICIENT_POWER` is a
gate rather than a footnote: below the power threshold the point estimates are not
evidence and downstream reports must not present them as such.
