"""Regression: the seniority term must not silently collapse to its default.

Background. ``KapInsiderTransaction.insider_role`` was never populated - the KAP
Pay Alim Satim Bildirimi form carries no job title - so every role reaching the
scorer was None, ``_role_score`` returned ``_DEFAULT_SENIORITY = 0.5`` every time,
and the ``_SENIORITY_MAP`` never fired. Combined with recency being pinned at 1.0
in historical mode, half the score's weight (0.30 seniority + 0.20 recency) was a
constant, leaving cluster_score a monotone function of insider_count alone.

The bug was invisible because the score still *looked* like a blended number. These
tests make the collapse observable: if roles ever stop resolving, the equivalence
below starts holding again and the suite fails.
"""
from decimal import Decimal

from trailing_edge.signals.cluster import compute_cluster_score

WEIGHTS = {"insider_count": 0.5, "role_seniority": 0.3, "recency": 0.2}
WINDOW = 30


def _score(roles, count=2, gap=7):
    return compute_cluster_score(
        insider_count=count,
        insider_roles=roles,
        days_since_last_buy=gap,
        weights=WEIGHTS,
        window_days=WINDOW,
    )


def test_reproduces_the_degenerate_score_from_the_committed_sample():
    """The committed sample still scores with seniority pinned at its 0.5 default,
    because person_company_roles is effectively empty in production - the report logs
    `role_map_empty` every run. Under that default the score collapses to:

        (0.25 * 0.5) + (0.5 * 0.3) + (0.7667 * 0.2) = 0.428333

    Reproducing it from all-None roles is the proof the map is dead. Until
    `graph scrape-management` populates the roster, cluster_score is insider_count wearing
    a decimal point.
    """
    assert _score([None, None]) == Decimal("42.8333")


def test_a_resolved_senior_role_moves_the_score():
    """The fix's whole purpose: a CEO must not score the same as an unknown."""
    unknown = _score([None, None])
    ceo = _score(["Genel Müdür", None])
    assert ceo > unknown
    assert ceo != Decimal("42.8333")


def test_seniority_map_ranks_roles():
    ceo = _score(["Genel Müdür"])
    board_member = _score(["Yönetim Kurulu Üyesi"])
    director = _score(["Direktör"])
    assert ceo > board_member > director


def test_seniority_takes_the_most_senior_insider_in_the_cluster():
    # max() across the cluster, so one CEO lifts a cluster of juniors.
    assert _score(["Direktör", "Genel Müdür"]) == _score(["Genel Müdür", "Direktör"])
    assert _score(["Direktör", "Genel Müdür"]) == _score(["Genel Müdür"])


def test_unknown_role_scores_below_every_known_role():
    """_DEFAULT_SENIORITY (0.5) sits BELOW the map's lowest entry ("direktör", 0.6).

    So an unresolved role is not scored neutrally - it is scored conservatively, as
    less senior than anyone the map recognises. That is a defensible choice (an
    unknown insider should not inflate a cluster), but it is only defensible while
    it is deliberate. Before the role join existed, no role ever resolved, so every
    cluster in production was pinned to this floor.
    """
    unknown = _score([None])
    assert unknown < _score(["Direktör"]) < _score(["Genel Müdür"])
