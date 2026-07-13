"""Layer A: insider cluster detection engine."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert

from trailing_edge.core.config import get_config
from trailing_edge.core.db import get_session
from trailing_edge.core.logging import get_logger
from trailing_edge.models.kap import KapInsiderTransaction
from trailing_edge.models.signal import InsiderCluster
from trailing_edge.signals.roles import resolve_roles_for_ticker, role_coverage

_log = get_logger(__name__)

# Role seniority map: (keywords, score) - first match wins, case-insensitive substring
_SENIORITY_MAP: list[tuple[list[str], float]] = [
    (["genel müdür", "ceo"], 1.0),
    (["mali işler", "cfo", "finans direktör"], 0.9),
    (["yönetim kurulu başkan"], 0.85),
    (["yönetim kurulu üye", "yk üye"], 0.7),
    (["genel müdür yardımcı", "gmy"], 0.65),
    (["direktör", "müdür"], 0.6),
]
_DEFAULT_SENIORITY = 0.5


def _role_score(role: str | None) -> float:
    if not role:
        return _DEFAULT_SENIORITY
    role_lower = role.lower()
    for keywords, score in _SENIORITY_MAP:
        if any(kw in role_lower for kw in keywords):
            return score
    return _DEFAULT_SENIORITY


def compute_cluster_score(
    insider_count: int,
    insider_roles: list[str | None],
    days_since_last_buy: int,
    weights: dict[str, float],
    window_days: int,
) -> Decimal:
    """
    Weighted cluster score in range [0.0000, 100.0000].

    insider_count_score:  min((count-1)/4, 1.0)  → 2=0.25, 5+=1.0
    role_seniority_score: max role score across insiders, default 0.5 when all None
    recency_score:        1 - days_since_last_buy/window_days (0 when gap=0, 0 when gap>=window)
    """
    insider_count_score = min((insider_count - 1) / 4.0, 1.0)

    role_seniority_score = max(
        (_role_score(r) for r in insider_roles),
        default=_DEFAULT_SENIORITY,
    )

    recency_score = max(0.0, 1.0 - days_since_last_buy / window_days)

    raw = (
        insider_count_score * weights["insider_count"]
        + role_seniority_score * weights["role_seniority"]
        + recency_score * weights["recency"]
    ) * 100.0

    return Decimal(str(raw)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _find_cluster_events(
    txs: list,
    window_days: int,
    min_count: int,
    as_of_date: date | None,
) -> list[tuple[date, date, set[str], list]]:
    """
    For same-ticker BUY transactions (sorted by date), return cluster formation events.

    For each transaction T, scan the look-back window [T.date - window_days, T.date].
    If distinct insiders >= min_count, emit one event per unique (window_start, window_end).
    seen_keys deduplicates within a single call to avoid redundant upserts.
    """
    events: list[tuple[date, date, set[str], list]] = []
    seen_keys: set[tuple[date, date]] = set()

    for tx in txs:
        current_date = tx.transaction_date
        if as_of_date is not None and current_date > as_of_date:
            break

        window_cutoff = current_date - timedelta(days=window_days)
        window_txs = [t for t in txs if window_cutoff <= t.transaction_date <= current_date]
        distinct = {t.insider_name for t in window_txs}

        if len(distinct) < min_count:
            continue

        ws = min(t.transaction_date for t in window_txs)
        we = current_date
        key = (ws, we)

        if key not in seen_keys:
            seen_keys.add(key)
            events.append((ws, we, distinct, window_txs))

    return events


async def detect_clusters(as_of_date: date | None = None) -> list[InsiderCluster]:
    """
    Find all insider cluster events and upsert to insider_clusters.

    as_of_date=None: scan ALL BUY transactions in history (historical mode).
      Each cluster scored with days_since_last_buy=0 (detected at formation time).
    as_of_date=X: scan only [X - window_days, X] (live/rolling mode).
      Recency reflects how recently the last buy occurred relative to X.

    Returns list of upserted InsiderCluster records.
    """
    cfg = get_config()["signals"]["cluster"]
    window_days: int = cfg["window_days"]
    min_count: int = cfg["min_insider_count"]
    weights: dict = cfg["score_weights"]

    stmt = select(
        KapInsiderTransaction.ticker,
        KapInsiderTransaction.insider_name,
        KapInsiderTransaction.insider_role,
        KapInsiderTransaction.transaction_date,
        KapInsiderTransaction.share_count,
        KapInsiderTransaction.price_try,
        KapInsiderTransaction.total_value_try,
    ).where(KapInsiderTransaction.transaction_type == "BUY")

    if as_of_date is not None:
        cutoff = as_of_date - timedelta(days=window_days)
        stmt = stmt.where(
            KapInsiderTransaction.transaction_date >= cutoff,
            KapInsiderTransaction.transaction_date <= as_of_date,
        )

    stmt = stmt.order_by(
        KapInsiderTransaction.ticker, KapInsiderTransaction.transaction_date
    )

    async with get_session() as session:
        rows = (await session.execute(stmt)).all()

        ticker_txs: dict[str, list] = defaultdict(list)
        for row in rows:
            ticker_txs[row.ticker].append(row)

        cluster_keys: list[tuple[str, date, date]] = []
        coverage_seen: list[float] = []

        for ticker, txs in ticker_txs.items():
            events = _find_cluster_events(txs, window_days, min_count, as_of_date)
            if not events:
                continue

            # The KAP insider form carries no job title, so tx.insider_role is
            # always None. Seniority has to come from the scraped board/executive
            # roster (person_company_roles). Without this join the seniority term
            # silently collapses to its 0.5 default for every cluster - see
            # signals/roles.py.
            all_names = sorted({t.insider_name for t in txs})
            resolved = await resolve_roles_for_ticker(ticker, all_names, session)

            for window_start, window_end, distinct_insiders, event_txs in events:
                unique_insiders = sorted(distinct_insiders)
                insider_roles = [
                    resolved.get(t.insider_name) or t.insider_role for t in event_txs
                ]
                coverage_seen.append(role_coverage(resolved, unique_insiders))

                # Historical mode: recency=1.0 (scored at formation time)
                # Live mode: recency based on gap from as_of_date to window_end
                score_as_of = as_of_date if as_of_date is not None else window_end
                days_since = (score_as_of - window_end).days

                score = compute_cluster_score(
                    insider_count=len(distinct_insiders),
                    insider_roles=insider_roles,
                    days_since_last_buy=days_since,
                    weights=weights,
                    window_days=window_days,
                )

                # total_buy_value: prefer pre-computed total_value_try, else price*count
                total_value: Decimal | None = None
                value_parts = [
                    t.total_value_try
                    if t.total_value_try is not None
                    else (t.price_try * t.share_count if t.price_try is not None else None)
                    for t in event_txs
                ]
                non_null = [v for v in value_parts if v is not None]
                if non_null:
                    total_value = sum(non_null)

                insert_stmt = (
                    pg_insert(InsiderCluster)
                    .values(
                        ticker=ticker,
                        window_start=window_start,
                        window_end=window_end,
                        insider_count=len(distinct_insiders),
                        unique_insiders=unique_insiders,
                        total_buy_value_try=total_value,
                        cluster_score=score,
                    )
                    .on_conflict_do_update(
                        constraint="uq_cluster_ticker_window",
                        set_={
                            "insider_count": len(distinct_insiders),
                            "unique_insiders": unique_insiders,
                            "total_buy_value_try": total_value,
                            "cluster_score": score,
                        },
                    )
                )
                await session.execute(insert_stmt)
                cluster_keys.append((ticker, window_start, window_end))

                _log.info(
                    "cluster_upserted",
                    ticker=ticker,
                    window_start=str(window_start),
                    window_end=str(window_end),
                    insider_count=len(distinct_insiders),
                    score=float(score),
                )

        # A dead role map is not a cosmetic problem: it pins the seniority term
        # (weight 0.30) at its 0.5 default and reduces cluster_score to a monotone
        # function of insider_count. That must never again happen quietly.
        mean_coverage = (
            sum(coverage_seen) / len(coverage_seen) if coverage_seen else 0.0
        )
        if coverage_seen and mean_coverage == 0.0:
            _log.warning(
                "role_map_empty",
                clusters=len(coverage_seen),
                hint=(
                    "no insider matched person_company_roles; seniority term is a "
                    "constant and cluster_score is effectively insider_count alone. "
                    "Run `kap management backfill` to populate the roster."
                ),
            )
        else:
            _log.info("role_coverage", mean_pct=round(mean_coverage * 100, 1))

        if not cluster_keys:
            _log.info("detect_clusters_done", found=0)
            return []

        # Fetch back only the clusters that were actually upserted in this run
        result = await session.execute(
            select(InsiderCluster).where(
                tuple_(
                    InsiderCluster.ticker,
                    InsiderCluster.window_start,
                    InsiderCluster.window_end,
                ).in_(cluster_keys)
            )
        )
        clusters = result.scalars().all()

    _log.info("detect_clusters_done", found=len(clusters))
    return list(clusters)
