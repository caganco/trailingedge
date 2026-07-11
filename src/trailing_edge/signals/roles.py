"""Resolve insider roles for cluster scoring.

The bug this fixes
------------------
``KapInsiderTransaction.insider_role`` is declared on the model and threaded
through the repository, but the DKB PDF parser never populates it - the KAP
Pay Alim Satim Bildirimi form simply does not carry a job title. So every
transaction reached the scorer with ``insider_role=None``.

The consequence was silent and total: ``cluster._role_score`` falls back to
``_DEFAULT_SENIORITY = 0.5`` for a None role, so the ``_SENIORITY_MAP`` never
fired once and the role_seniority term (weight 0.30) was the constant 0.5 for
every cluster ever scored. In historical mode ``recency`` is likewise pinned at
1.0 (weight 0.20). Half the score's weight was therefore a fixed offset, leaving
``cluster_score`` a monotone function of ``insider_count`` alone - a
single-factor score wearing a three-factor costume.

Proof from the committed sample (reports/sample/daily_signal.example.json):
insider_count=2, days_since_last_buy=7, window_days=30 gives
    (0.25 * 0.5) + (0.5 * 0.3) + (0.7667 * 0.2) = 0.428333 -> 42.8333
which is the exact score in that file. The 0.5 is the default, not a lookup.

The fix
-------
The role data already exists: ``scrapers/kap/management.py`` scrapes each
company's board and executives into ``person_company_roles`` (source
KAP_YONETIM). It was simply never joined to the insider transactions. This
module does that join, keyed on the same ``normalize_name`` used to build
``persons.name_normalized``, and scoped to the company the trade was in - a
person can sit on several boards with different titles.

Roles that cannot be resolved stay ``None`` and still score the 0.5 default,
which is correct (unknown seniority is not evidence of low seniority). What is
no longer acceptable is not knowing that this happened, so callers surface the
coverage ratio and ``detect_clusters`` warns loudly when it is zero.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trailing_edge.models.graph import Company, Person, PersonCompanyRole
from trailing_edge.scrapers.kap.helpers import normalize_name


async def resolve_roles_for_ticker(
    ticker: str,
    insider_names: list[str],
    session: AsyncSession,
) -> dict[str, str | None]:
    """
    Map insider_name -> role title, for insiders holding a role at ``ticker``.

    Returns a dict keyed by the ORIGINAL insider name (as stored on the
    transaction), so callers do not have to re-normalize. Names with no
    resolvable role are absent from the map, not present with None: an absent
    key means "unknown", and the caller decides what to do with that.
    """
    if not insider_names:
        return {}

    norm_to_original: dict[str, str] = {}
    for name in insider_names:
        norm_to_original.setdefault(normalize_name(name), name)

    stmt = (
        select(Person.name_normalized, PersonCompanyRole.role)
        .join(PersonCompanyRole, PersonCompanyRole.person_id == Person.id)
        .join(Company, Company.id == PersonCompanyRole.company_id)
        .where(
            Company.ticker == ticker,
            Person.name_normalized.in_(list(norm_to_original)),
            PersonCompanyRole.role.is_not(None),
        )
    )
    rows = (await session.execute(stmt)).all()

    out: dict[str, str | None] = {}
    for name_norm, role in rows:
        original = norm_to_original.get(name_norm)
        if original is not None:
            out[original] = role
    return out


def role_coverage(resolved: dict[str, str | None], insider_names: list[str]) -> float:
    """Fraction of insiders whose role was resolved. 0.0 means the map is dead."""
    if not insider_names:
        return 0.0
    return len([n for n in insider_names if resolved.get(n)]) / len(insider_names)
