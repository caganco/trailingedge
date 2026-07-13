"""Routine/opportunistic classifier - the pre-registered test's implementation.

Definition frozen in docs/stage0/OPPORTUNISTIC_CLASSIFIER.md before any result was
computed. These tests pin it, so that a later reading of the result cannot quietly become
a later reading of the definition.
"""
from datetime import date

from trailing_edge.signals.opportunistic import (
    InsiderClass,
    classify_cluster,
    classify_insider,
)

AS_OF = date(2018, 6, 15)


def test_no_prior_purchase_is_unclassified_not_opportunistic():
    """CMP's result rests on a trading history. An insider without one is evidence for
    neither side, and folding them into either group would be choosing an answer."""
    assert classify_insider([], AS_OF) is InsiderClass.UNCLASSIFIED


def test_a_purchase_older_than_the_lookback_does_not_count():
    stale = [date(2014, 3, 1)]  # > 36 months before AS_OF
    assert classify_insider(stale, AS_OF) is InsiderClass.UNCLASSIFIED


def test_the_same_month_every_year_is_routine():
    """The definition of routine: on a schedule, not on information."""
    every_march = [date(2016, 3, 10), date(2017, 3, 14), date(2018, 3, 9)]
    assert classify_insider(every_march, AS_OF) is InsiderClass.ROUTINE


def test_scattered_months_are_opportunistic():
    scattered = [date(2016, 2, 3), date(2017, 7, 21), date(2018, 5, 4)]
    assert classify_insider(scattered, AS_OF) is InsiderClass.OPPORTUNISTIC


def test_too_few_purchase_months_cannot_be_routine():
    """Routine needs >= 3 distinct purchase months. Two Marches is a coincidence, not a
    schedule - and calling it routine would strip an informative trade."""
    two_marches = [date(2017, 3, 8), date(2018, 3, 12)]
    assert classify_insider(two_marches, AS_OF) is InsiderClass.OPPORTUNISTIC


def test_seasonal_share_must_clear_60_percent():
    """3 of 5 purchase months in March is 60% - routine. 2 of 5 is not."""
    mostly_march = [
        date(2016, 3, 1), date(2017, 3, 1), date(2018, 3, 1),
        date(2016, 8, 1), date(2017, 11, 1),
    ]
    assert classify_insider(mostly_march, AS_OF) is InsiderClass.ROUTINE

    barely_march = [
        date(2016, 3, 1), date(2017, 3, 1),
        date(2016, 8, 1), date(2017, 11, 1), date(2018, 1, 1),
    ]
    assert classify_insider(barely_march, AS_OF) is InsiderClass.OPPORTUNISTIC


def test_repeated_purchases_in_one_month_count_once():
    """Buying four times in one March is one purchase month, not four - otherwise a
    single busy month would masquerade as a schedule."""
    one_busy_march = [date(2018, 3, d) for d in (1, 8, 15, 22)]
    assert classify_insider(one_busy_march, AS_OF) is InsiderClass.OPPORTUNISTIC


def test_a_future_purchase_is_ignored():
    """Nothing after as_of may be seen. The caller filters on published_at, but the
    classifier must not lean on that."""
    with_future = [date(2016, 2, 1), date(2017, 7, 1), date(2019, 5, 1)]
    assert classify_insider(with_future, AS_OF) is InsiderClass.OPPORTUNISTIC


# --- cluster-level ----------------------------------------------------------

def test_one_opportunistic_insider_makes_the_cluster_opportunistic():
    """A cluster is a co-purchase event; one informed participant is enough to make it
    informative."""
    got = classify_cluster([InsiderClass.ROUTINE, InsiderClass.OPPORTUNISTIC])
    assert got is InsiderClass.OPPORTUNISTIC


def test_all_routine_makes_the_cluster_routine():
    got = classify_cluster([InsiderClass.ROUTINE, InsiderClass.ROUTINE])
    assert got is InsiderClass.ROUTINE


def test_unclassified_never_upgrades_a_cluster():
    assert classify_cluster([InsiderClass.UNCLASSIFIED]) is InsiderClass.UNCLASSIFIED
    assert (
        classify_cluster([InsiderClass.UNCLASSIFIED, InsiderClass.ROUTINE])
        is InsiderClass.ROUTINE
    )


def test_empty_cluster_is_unclassified():
    assert classify_cluster([]) is InsiderClass.UNCLASSIFIED
