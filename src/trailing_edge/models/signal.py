"""ORM models for Layer A signal data: prices, clusters, outcomes."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, TEXT, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from trailing_edge.models.base import Base


class PriceHistory(Base):
    __tablename__ = "price_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(50), nullable=False)
    price_date: Mapped[date] = mapped_column(Date, nullable=False)
    open_try: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    high_try: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    low_try: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    close_try: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    volume: Mapped[int | None] = mapped_column(BigInteger)
    fetched_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("ticker", "price_date", name="uq_price_ticker_date"),
        Index("idx_price_ticker_date", "ticker", "price_date"),
    )


class InsiderCluster(Base):
    __tablename__ = "insider_clusters"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(50), nullable=False)
    window_start: Mapped[date] = mapped_column(Date, nullable=False)
    window_end: Mapped[date] = mapped_column(Date, nullable=False)
    insider_count: Mapped[int] = mapped_column(Integer, nullable=False)
    unique_insiders: Mapped[list[str]] = mapped_column(ARRAY(TEXT), nullable=False)
    total_buy_value_try: Mapped[Decimal | None] = mapped_column(Numeric(24, 2))
    cluster_score: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    outcomes: Mapped[list["SignalOutcome"]] = relationship(
        back_populates="cluster", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("ticker", "window_start", "window_end", name="uq_cluster_ticker_window"),
        Index("idx_cluster_ticker", "ticker"),
        Index("idx_cluster_detected", "detected_at"),
        Index("idx_cluster_score", "cluster_score"),
    )


class SignalOutcome(Base):
    __tablename__ = "signal_outcomes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cluster_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("insider_clusters.id", ondelete="CASCADE"), nullable=False
    )
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    return_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))

    # Actual traded dates, stored so the market adjustment is auditable: the
    # benchmark is priced on THESE dates, not on its own trading-day offsets.
    entry_date: Mapped[date | None] = mapped_column(Date)
    exit_date: Mapped[date | None] = mapped_column(Date)

    # Market adjustment (see signals/abnormal.py). abnormal_return_pct is the
    # PRIMARY metric; return_pct alone credits market drift and beta to the
    # signal and must not be reported as evidence of an edge.
    benchmark_ticker: Mapped[str | None] = mapped_column(String(50))
    benchmark_return_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    abnormal_return_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))

    calculated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    cluster: Mapped["InsiderCluster"] = relationship(back_populates="outcomes")

    __table_args__ = (
        UniqueConstraint("cluster_id", "horizon_days", name="uq_outcome_cluster_horizon"),
    )
