"""Add VBTS tradability flags to price_history.

Borsa Istanbul's Volatilite Bazli Tedbir Sistemi escalates measures on a volatile
stock: short-selling ban, then GROSS SETTLEMENT (brut takas), then a single-price
auction. A name under gross settlement cannot be entered and exited the way a
backtest assumes, and a suspended name cannot be traded at all - yet the pipeline
has been booking entries in both.

The exchange's own bulletin carries these as columns (BRUT TAKAS, GECICI DURDURMA),
so the filter costs nothing but reading them.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-13
"""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "price_history",
        sa.Column("gross_settlement", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "price_history",
        sa.Column("suspended", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("price_history", "suspended")
    op.drop_column("price_history", "gross_settlement")
