"""Keep the traded close alongside the total-return index.

`price_history.close_try` holds a chained, corporate-action-adjusted index, not the
price the stock actually printed. That is the right series for returns - a bonus issue
halves the print and a raw series reads it as a 50% loss - and it is what every return
in this project is computed from.

But two costs are properties of the PRICE, not of the index:

  - the tick floor is 0.01 TRY on a grid, so it depends on where the stock actually
    trades. BIST companies issue bonus shares constantly; on 2018-12 data the index sat
    at a median 0.98x the traded price but ranged from 0.60x to 118x, and 32% of
    ticker-days were off by more than 10%. Feeding the index to `tick_floor_pct` divided
    the tick cost by that factor.
  - ADV in TRY is price x volume. The same factor inflated it, and Kyle impact scales
    with sqrt(1/ADV), so impact came out understated too.

Both errors land in the cost model, which is the module that decides whether the signal
is tradeable. The factor is not one-directional, so this is not a conservative error - it
mispriced individual trades in both directions, worst in the serial bonus-issuers, which
are small caps, which are exactly where the tick floor binds.

So the raw close is now kept. The index stays the default for returns; the cost model
reads the price.

Revision ID: 0008
Revises: 0007
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable with no backfill: a NULL here means "this row predates the fix and its
    # traded price was never stored". The cost model must see that and refuse, rather
    # than silently fall back to the index - falling back is the bug being fixed.
    op.add_column(
        "price_history",
        sa.Column("raw_close_try", sa.Numeric(20, 4), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("price_history", "raw_close_try")
