"""GraphRepository.upsert_management_roles, without touching the network.

The only existing coverage for this path (test_management_scrape_single_company_idempotent)
scrapes KAP live, so it cannot run while the WAF is blocking - and that left the two
INSERT statements inside it effectively untested. Both were rewritten from
`insert(Person.__table__)` to `insert(Person)` to satisfy the type checker, and a type
checker proves nothing about whether a row still lands.

This exercises the same code against synthetic members: rows are written, and a re-run
with the same input is idempotent (the method DELETEs its own source rows before
inserting, because a NULL valid_from makes ON CONFLICT never fire).
"""
from datetime import date

import pytest
from sqlalchemy import delete, func, select

from trailing_edge.models.graph import Company, Person, PersonCompanyRole
from trailing_edge.scrapers.kap.types import BoardMemberDTO
from trailing_edge.storage.repository import GraphRepository

_TICKER = "ZZBOARD"

_MEMBERS = [
    BoardMemberDTO(
        full_name="ZZ Test Chairman",
        role="Yönetim Kurulu Başkanı",
        role_type="BOARD_CHAIR",
        is_independent=False,
        valid_from=date(2019, 1, 1),
    ),
    BoardMemberDTO(
        full_name="ZZ Test Member",
        role="Yönetim Kurulu Üyesi",
        role_type="BOARD_MEMBER",
        is_independent=True,
        valid_from=None,  # the NULL that defeats ON CONFLICT
    ),
]


async def _count_roles(session, company_id: int) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(PersonCompanyRole)
            .where(
                PersonCompanyRole.company_id == company_id,
                PersonCompanyRole.source == "KAP_YONETIM",
            )
        )
    ).scalar_one()


async def _cleanup(session) -> None:
    cid = (
        await session.execute(select(Company.id).where(Company.ticker == _TICKER))
    ).scalar()
    if cid is not None:
        await session.execute(
            delete(PersonCompanyRole).where(PersonCompanyRole.company_id == cid)
        )
        await session.execute(delete(Company).where(Company.id == cid))
    await session.execute(
        delete(Person).where(Person.full_name.in_([m.full_name for m in _MEMBERS]))
    )


@pytest.mark.asyncio
async def test_roles_are_written_and_rewriting_them_is_idempotent(db_session):
    await _cleanup(db_session)

    db_session.add(Company(ticker=_TICKER, company_name="ZZ Board Test AS"))
    await db_session.flush()
    company_id = (
        await db_session.execute(select(Company.id).where(Company.ticker == _TICKER))
    ).scalar_one()

    repo = GraphRepository(db_session)

    first = await repo.upsert_management_roles(_TICKER, _MEMBERS)
    await db_session.flush()
    count1 = await _count_roles(db_session, company_id)

    assert first.inserted == len(_MEMBERS), "insert(Person)/insert(PersonCompanyRole) wrote nothing"
    assert count1 == len(_MEMBERS)

    # Re-running must not duplicate, even though valid_from is NULL on one member and a
    # UNIQUE constraint therefore treats every NULL as distinct.
    await repo.upsert_management_roles(_TICKER, _MEMBERS)
    await db_session.flush()
    count2 = await _count_roles(db_session, company_id)

    assert count2 == count1, "re-scrape duplicated the board"

    await _cleanup(db_session)


@pytest.mark.asyncio
async def test_an_unknown_ticker_is_a_no_op_not_a_crash(db_session):
    repo = GraphRepository(db_session)
    got = await repo.upsert_management_roles("ZZNOSUCH", _MEMBERS)
    assert got.inserted == 0
