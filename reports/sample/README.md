# Sample daily signal report

`daily_signal.example.json` is a faithful example of what `trailingedge signal daily-report`
emits. The cluster is anonymized (ticker and insider names); every statistic in it is real
and reproducible from the pipeline.

## Read the two verdicts together, or you will read it wrong

The file carries two, and they disagree on purpose:

```json
"base_rates":    { "20": { "verdict": "EDGE_DETECTED", ... } }
"cost_adjusted": {        "verdict": "LOSES_MONEY_NET_OF_COST", ... }
```

`compute_base_rate` measures the **gross** abnormal return and knows nothing about the
bid-ask spread. On this sample it is right: +2.07% at 20 days, t = 5.36, N = 1,079,
survivorship-clean. That is a real, statistically strong signal.

It is also uncapturable. Insider clusters fire in illiquid BIST small caps whose round
trip costs a median 1.93% — wider than the alpha. Net of cost the same sample returns
**−1.35% at 20 days (t = −3.31)**.

A report that printed `EDGE_DETECTED` and a 54.7% hit rate and stopped there would have
this tool contradict its own project's conclusion, and a reader would trade it. So the net
result is emitted in the same document and printed on the same screen, rather than left in
a methodology file nobody opens. The gross number is not the finding; the gap between the
two numbers is.

## `cluster_score` is not yet a real score

The sample scores 46.17 and 45.50. Both are computed with the seniority term pinned at its
0.5 default, because `person_company_roles` is empty — the report logs `role_map_empty` on
every run. Until `graph scrape-management` populates the board roster, `cluster_score` is
`insider_count` with a decimal point on it, and it is documented as such in
[`docs/METHODOLOGY.md`](../../docs/METHODOLOGY.md#6-still-open) rather than presented as a
model.

## What the first version of this file claimed, and why it was void

For the record, since the numbers were public:

| Horizon | N | Hit rate | Mean **raw** return |
|--------:|--:|---------:|--------------------:|
| 5d  | 29 | 44.83% | −2.00% |
| 20d | 29 | 55.17% | −0.48% |
| 60d | 23 | 52.17% | +5.91% |

Two things made those figures unusable, and both are fixed in code:

**They were raw returns, not abnormal returns.** BIST quotes nominal TRY under high
inflation, so a randomly chosen stock rises well over half the time and its mean return is
not zero. A hit rate scored against a 50% coin-flip prior and a mean scored against 0%
hand the signal credit for market drift. The 60-day `+5.91%` in particular is comfortably
inside what the index alone returned over the same span. See
`src/trailing_edge/signals/abnormal.py`.

**N was 29.** The 95% Wilson interval around a 55.17% hit rate is roughly **[37%, 72%]** —
it contains the 50% null with room to spare. Separating a 55% hit rate from 50% at α=0.05
with 80% power needs

```
n = (1.96 + 0.84)² × 0.25 / 0.05² ≈ 784
```

observations. At N=29 the sample could not distinguish this signal from a coin in *either*
direction. Publishing the point estimate without that interval was the actual error; the
number itself was never the problem.

N was never 29 as a fact about the market. It was the sum of the silent data faults listed
in the top-level [README](../../README.md) — each of which had moved it. After they were
fixed, N is 1,079.
