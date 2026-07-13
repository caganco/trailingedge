"""KAP's relatedStocks field, which is not always a single ticker.

The field was consumed as a plain string, so a filing naming several stocks became a
"ticker" like `KRDMA, KRDMB, KRDMD` - a key that joins to no price row. The clusters did
not error, they just vanished from every result: 14 of them, on 12 distinct malformed
strings across ~131 disclosures.
"""
from trailing_edge.scrapers.kap.parser import (
    normalize_related_tickers,
    split_related_tickers,
)


def test_a_single_ticker_is_unchanged():
    assert split_related_tickers("THYAO") == ["THYAO"]
    assert split_related_tickers("  thyao  ") == ["THYAO"]


def test_share_classes_of_one_issuer_are_split():
    """Kardemir lists three classes; the filing names all three."""
    assert split_related_tickers("KRDMA, KRDMB, KRDMD") == ["KRDMA", "KRDMB", "KRDMD"]


def test_two_different_issuers_are_split():
    """Not every multi-ticker filing is one company's share classes - a filer tied to two
    issuers produces two unrelated tickers, which is why the class cannot be guessed."""
    assert split_related_tickers("ANELT, VERTU") == ["ANELT", "VERTU"]


def test_empty_and_blank_yield_nothing():
    assert split_related_tickers("") == []
    assert split_related_tickers("  ,  ") == []


def test_normalized_form_is_whitespace_free_and_order_preserving():
    """The ambiguity stays in the data rather than being laundered into one plausible
    ticker. Canonical form so the same filing always keys the same way."""
    assert normalize_related_tickers("KRDMA, KRDMB, KRDMD") == "KRDMA,KRDMB,KRDMD"
    assert normalize_related_tickers("krdma ,krdmb") == "KRDMA,KRDMB"


def test_splitting_never_silently_picks_one():
    """The whole point. A function that returned 'KRDMA' here would put a real insider
    purchase on a stock the insider may never have touched - and it would join to a price,
    so nothing downstream would ever notice."""
    got = split_related_tickers("KRDMA, KRDMB, KRDMD")
    assert len(got) == 3, "attribution must be left to the caller, not guessed here"
