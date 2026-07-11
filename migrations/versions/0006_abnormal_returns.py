"""Add market-adjusted (abnormal) return columns to signal_outcomes.

A raw forward return credits market drift and beta to the signal. The primary
evidence metric becomes abnormal_return_pct = return_pct - benchmark_return_pct,
measured over the same held interval (entry_date .. exit_date).

Existing rows are left with NULL in the new columns rather than backfilled with
a guessed benchmark: a NULL that forces a recompute is safer than a plausible
wrong number. Re-run `calculate_outcomes` to populate.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-12
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("signal_outcomes", sa.Column("entry_date", sa.Date(), nullable=True))
    op.add_column("signal_outcomes", sa.Column("exit_date", sa.Date(), nullable=True))
    op.add_column(
        "signal_outcomes", sa.Column("benchmark_ticker", sa.String(50), nullable=True)
    )
    op.add_column(
        "signal_outcomes", sa.Column("benchmark_return_pct", sa.Numeric(10, 4), nullable=True)
    )
    op.add_column(
        "signal_outcomes", sa.Column("abnormal_return_pct", sa.Numeric(10, 4), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("signal_outcomes", "abnormal_return_pct")
    op.drop_column("signal_outcomes", "benchmark_return_pct")
    op.drop_column("signal_outcomes", "benchmark_ticker")
    op.drop_column("signal_outcomes", "exit_date")
    op.drop_column("signal_outcomes", "entry_date")
