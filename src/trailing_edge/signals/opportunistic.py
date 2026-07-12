"""Routine vs opportunistic insiders (Cohen-Malloy-Pomorski, adapted to BIST).

The definition is FROZEN in docs/stage0/OPPORTUNISTIC_CLASSIFIER.md and was committed
before any result was computed. This module implements it and introduces no parameter of
its own. If the definition must change, it changes there, with a reason, and the old
result is reported alongside the new one - not replaced by it.

Why the US test cannot be ported as-is: CMP call an insider routine if they traded in the
same calendar month for three consecutive years, and refuse to classify anyone with less
than three years of history. Under SPK II-15.1 Art. 11 a Turkish insider owes no
disclosure until their transactions cross 250,000 TRY within the calendar year, so filing
histories are sparse and gappy. Demanding three consecutive years would classify almost
nobody - and would select on filing frequency, which tracks position size, which tracks
the outcome.
"""
from __future__ import annotations

from collections import Counter
from datetime import date
from enum import Enum

# --- frozen parameters (docs/stage0/OPPORTUNISTIC_CLASSIFIER.md) ---
LOOKBACK_MONTHS = 36
MIN_PURCHASE_MONTHS = 3
SEASONAL_SHARE = 0.60


class InsiderClass(str, Enum):
    ROUTINE = "ROUTINE"
    OPPORTUNISTIC = "OPPORTUNISTIC"
    UNCLASSIFIED = "UNCLASSIFIED"


def _months_before(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 - months, 12)
    return date(y, m + 1, 1)


def classify_insider(
    prior_purchase_dates: list[date],
    as_of: date,
) -> InsiderClass:
    """Classify one insider at one point in time.

    ``prior_purchase_dates`` must already be filtered to filings whose disclosure was
    PUBLIC at or before ``as_of`` - this function cannot check that, and passing
    transaction dates instead of disclosure-visible ones would reintroduce look-ahead
    through the back door.

    ROUTINE: filed purchases in >= 3 distinct calendar months over the trailing 36, and
    >= 60% of those months land on the same month of the year (an insider who buys every
    March is on a schedule, not on information).

    OPPORTUNISTIC: has at least one prior purchase in the window and is not routine.

    UNCLASSIFIED: no prior purchase in the window. Reported separately, never merged into
    either group - CMP's result rests on a trading history, and an insider without one is
    evidence for neither side.
    """
    cutoff = _months_before(as_of, LOOKBACK_MONTHS)
    window = [d for d in prior_purchase_dates if cutoff <= d <= as_of]
    if not window:
        return InsiderClass.UNCLASSIFIED

    purchase_months = {(d.year, d.month) for d in window}
    if len(purchase_months) < MIN_PURCHASE_MONTHS:
        return InsiderClass.OPPORTUNISTIC

    seasonal = Counter(m for _, m in purchase_months)
    top = seasonal.most_common(1)[0][1]
    if top / len(purchase_months) >= SEASONAL_SHARE:
        return InsiderClass.ROUTINE
    return InsiderClass.OPPORTUNISTIC


def classify_cluster(insider_classes: list[InsiderClass]) -> InsiderClass:
    """A cluster is opportunistic if ANY of its insiders is.

    A cluster is a co-purchase event; one informed participant is enough to make it
    informative. Requiring every insider to be opportunistic is a stricter, different
    test - it is deliberately NOT run, so that there is no second specification to pick
    between after the fact.
    """
    if any(c is InsiderClass.OPPORTUNISTIC for c in insider_classes):
        return InsiderClass.OPPORTUNISTIC
    if any(c is InsiderClass.ROUTINE for c in insider_classes):
        return InsiderClass.ROUTINE
    return InsiderClass.UNCLASSIFIED
