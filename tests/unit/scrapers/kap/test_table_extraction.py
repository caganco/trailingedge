"""Row extraction from the DKB transaction table.

Two bugs lived here, both silent, both costing years of history or corrupting the rows
that did survive. These tests pin the exact text shapes that broke them.
"""
from trailing_edge.scrapers.kap.parser import _extract_table_rows

# A pre-2021 filing: the date uses DOTS. The table is otherwise identical to a modern one
# (9 numeric columns). Taken from a real 2019 TUKAS filing.
LEGACY_2019 = """
Islem Tarihi
14.06.2019
1.845.337
0
1.845.337
107.266.000
109.111.337
39,34
39,34
40,02
40,02
( * ) Net satis olmasi durumunda net nominal tutar
"""

# A modern filing: the date uses SLASHES, and pdfminer has merged two adjacent cells
# onto one line ("174.004.552,79 174.269.552,79").
MODERN_2023_MERGED = """
Islem Tarihi
07/06/2023
265.000
0
265.000
174.004.552,79 174.269.552,79
49,72
49,72
49,79
49,79
"""

# The narrative price sentence. Its numbers must NEVER be mistaken for table cells.
NARRATIVE_PRICE = """
paylari 18,45 - 18,48 TL fiyat araligindan satilmistir
25/05/2026
0
2.500.000
2.500.000
50.000.000
47.500.000
20,10
20,10
19,10
19,10
"""


def test_dotted_dates_anchor_a_row():
    """The whole pre-2021 archive hung on this.

    _DATE_RE accepted slashes only, so no row in a 2016-2020 filing ever anchored,
    _extract_table_rows returned nothing, and every one of those filings parsed to ZERO
    transactions while the ingest reported success. Measured against live KAP, the fix
    took 2016-2020 from 0% parse yield to 75-100%.
    """
    rows = _extract_table_rows(LEGACY_2019)

    assert len(rows) == 1
    assert rows[0][0] == "14.06.2019"
    assert rows[0][1] == "1.845.337"  # buy nominal
    assert len(rows[0]) == 10  # date + 9 columns


def test_slashed_dates_still_anchor_a_row():
    """The modern format must keep working - the fix widens, it does not replace."""
    rows = _extract_table_rows(MODERN_2023_MERGED)

    assert len(rows) == 1
    assert rows[0][0] == "07/06/2023"


def test_merged_cells_are_split_so_columns_do_not_shift():
    """pdfminer sometimes puts two cells on one line.

    As a single token that line fails the number pattern, and the collector *skips*
    non-numeric tokens - so both values vanish and every later column shifts left by two.
    The row still parses, which is what makes it dangerous: post_tx_share_count and
    post_tx_ownership_pct come back as plausible-looking numbers read from the wrong
    cells (the source of the implausible_ownership_pct warnings).
    """
    rows = _extract_table_rows(MODERN_2023_MERGED)
    row = rows[0]

    assert row[4] == "174.004.552,79"  # start nominal - both halves survived
    assert row[5] == "174.269.552,79"  # end nominal, NOT shifted into by the merge
    assert row[8] == "49,79"  # end capital % lands in its own column
    assert len(row) == 10


def test_narrative_numbers_are_not_read_as_table_cells():
    """Splitting merged lines must not shred prose.

    An unconditional whitespace split turns "18,45 - 18,48 TL" into a numeric token
    "18,45", which the collector reads as the first table column - so share_count comes
    back as a unit price. Only fully-numeric lines may be split.
    """
    rows = _extract_table_rows(NARRATIVE_PRICE)

    assert len(rows) == 1
    assert rows[0][0] == "25/05/2026"
    assert rows[0][2] == "2.500.000"  # the real sell nominal
    assert "18,45" not in rows[0]
    assert "18,48" not in rows[0]


def test_text_with_no_table_yields_no_rows():
    assert _extract_table_rows("aciklama ekte yer almaktadir\nEk dosyalar\n1- nthol2.pdf") == []
