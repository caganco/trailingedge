"""Forward return calculation for insider clusters."""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy.dialects.postgresql import insert as pg_insert

from trailing_edge.core.config import get_config
from trailing_edge.core.db import get_session
from trailing_edge.core.logging import get_logger
from trailing_edge.data.prices import get_price_and_date_after_days, get_price_on_date
from trailing_edge.models.signal import InsiderCluster, SignalOutcome
from trailing_edge.signals.abnormal import abnormal_return, pct_return
from trailing_edge.signals.entry_timing import (
    ENTRY_OFFSET_TRADING_DAYS,
    entry_exit_offsets,
    resolve_cluster_signal_dates,
)

_log = get_logger(__name__)


def _benchmark_ticker() -> str:
    return get_config()["signals"]["returns"]["benchmark"]


async def calculate_outcomes(
    clusters: list[InsiderCluster],
    horizons: list[int],
) -> None:
    """
    For each cluster × horizon, calculate the look-ahead-safe forward return and
    upsert to signal_outcomes.

    Entry is keyed to the look-ahead-safe *signal date* — the latest public KAP
    disclosure (``published_at``, correction-aware) backing the cluster — NOT the
    private transaction ``window_end``. This removes the filing-lag look-ahead:
      entry_price = close on t+1 (first trading day after the signal date)
      exit_price  = close ``horizon`` trading days after entry
      return_pct  = (exit - entry) / entry * 100

    Every outcome is ALSO market-adjusted against the configured benchmark
    (XU100), priced on the position's actual entry_date/exit_date:
      abnormal_return_pct = return_pct - benchmark_return_pct

    ``abnormal_return_pct`` is the primary evidence metric. ``return_pct`` on its
    own credits BIST's nominal drift and the stock's beta to the signal and is
    retained for audit only — see ``signals/abnormal.py`` for why. A cluster whose
    benchmark leg cannot be priced gets a NULL abnormal return (and is excluded
    from the base rate) rather than silently falling back to the raw return.

    Clusters with no resolvable public disclosure are skipped (cannot be entered
    look-ahead-safely). A signal date earlier than ``window_end`` is impossible
    for valid data (a disclosure cannot predate the transactions it reports) and
    is treated as a hard look-ahead violation. exit_price=None when the horizon
    date is in the future or price data is missing. All operations share one
    session to avoid repeated connection acquisition.
    """
    benchmark = _benchmark_ticker()

    async with get_session() as session:
        signal_dates = await resolve_cluster_signal_dates(clusters, session)

        for cluster in clusters:
            signal_date = signal_dates.get(cluster.id)
            if signal_date is None:
                _log.warning(
                    "outcome_skipped_no_public_disclosure",
                    ticker=cluster.ticker,
                    window_end=str(cluster.window_end),
                )
                continue

            # Look-ahead guard: the public disclosure cannot predate the last
            # transaction it reports, so the signal date must be on/after
            # window_end. A violation means malformed data — fail loud rather
            # than silently emit a look-ahead-biased return.
            if signal_date < cluster.window_end:
                raise AssertionError(
                    f"look-ahead violation: signal_date {signal_date} < window_end "
                    f"{cluster.window_end} for {cluster.ticker}"
                )

            # Entry strictly AFTER the public-disclosure day (t+1).
            entry_price, entry_date = await get_price_and_date_after_days(
                cluster.ticker, signal_date, ENTRY_OFFSET_TRADING_DAYS, session=session
            )

            for horizon in horizons:
                exit_price: Decimal | None = None
                exit_date = None
                return_pct: Decimal | None = None
                bench_return_pct: Decimal | None = None
                abnormal_pct: Decimal | None = None

                if entry_price is not None and entry_price > 0 and entry_date is not None:
                    _, exit_offset = entry_exit_offsets(horizon)
                    exit_price, exit_date = await get_price_and_date_after_days(
                        cluster.ticker, signal_date, exit_offset, session=session
                    )
                    if exit_price is not None and exit_date is not None:
                        return_pct = pct_return(entry_price, exit_price)

                        # Market adjustment: price the benchmark on the position's
                        # OWN entry/exit dates, so both legs span the same held
                        # interval even if the stock was halted mid-window.
                        bench_entry = await get_price_on_date(
                            benchmark, entry_date, session=session
                        )
                        bench_exit = await get_price_on_date(
                            benchmark, exit_date, session=session
                        )
                        if (
                            bench_entry is not None
                            and bench_exit is not None
                            and bench_entry > 0
                        ):
                            bench_return_pct = pct_return(bench_entry, bench_exit)
                            abnormal_pct = abnormal_return(
                                entry_price, exit_price, bench_entry, bench_exit
                            )
                        else:
                            _log.warning(
                                "benchmark_unpriced",
                                benchmark=benchmark,
                                ticker=cluster.ticker,
                                entry_date=str(entry_date),
                                exit_date=str(exit_date),
                            )

                values = {
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "return_pct": return_pct,
                    "entry_date": entry_date,
                    "exit_date": exit_date,
                    "benchmark_ticker": benchmark,
                    "benchmark_return_pct": bench_return_pct,
                    "abnormal_return_pct": abnormal_pct,
                }

                stmt = (
                    pg_insert(SignalOutcome)
                    .values(cluster_id=cluster.id, horizon_days=horizon, **values)
                    .on_conflict_do_update(
                        constraint="uq_outcome_cluster_horizon", set_=values
                    )
                )
                await session.execute(stmt)

                _log.info(
                    "outcome_upserted",
                    ticker=cluster.ticker,
                    signal_date=str(signal_date),
                    window_end=str(cluster.window_end),
                    horizon=horizon,
                    return_pct=float(return_pct) if return_pct is not None else None,
                    abnormal_return_pct=(
                        float(abnormal_pct) if abnormal_pct is not None else None
                    ),
                )
