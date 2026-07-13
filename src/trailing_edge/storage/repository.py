"""KapRepository and GraphRepository: storage layer for KAP and graph data."""
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime, timezone
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from trailing_edge.core.logging import get_logger
from trailing_edge.models.graph import Company, Person, PersonCompanyRole
from trailing_edge.models.kap import KapDisclosure, KapInsiderTransaction, ScraperRun
from trailing_edge.scrapers.kap.helpers import normalize_name
from trailing_edge.scrapers.kap.types import BoardMemberDTO, KapDisclosureDTO, KapInsiderTxDTO

_log = get_logger(__name__)


@dataclass
class UpsertResult:
    inserted: int
    skipped: int


class KapRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def upsert_disclosure(self, dto: KapDisclosureDTO) -> tuple[KapDisclosure, bool]:
        stmt = (
            insert(KapDisclosure)
            .values(
                kap_disclosure_id=dto.kap_disclosure_id,
                ticker=dto.ticker,
                company_name=dto.company_name,
                disclosure_type=dto.disclosure_type,
                disclosure_subtype=dto.disclosure_subtype,
                disclosure_class=dto.disclosure_class,
                published_at=dto.published_at,
                is_correction=dto.is_correction,
                source_url=dto.source_url,
                raw_html=dto.raw_html,
                raw_json=dto.raw_json,
            )
            .on_conflict_do_update(
                index_elements=["kap_disclosure_id"],
                set_={
                    "company_name": dto.company_name,
                    "is_correction": dto.is_correction,
                    "raw_html": dto.raw_html,
                    "raw_json": dto.raw_json,
                    "updated_at": text("NOW()"),
                },
            )
            .returning(KapDisclosure.id, KapDisclosure.ingested_at, KapDisclosure.updated_at)
        )
        result = await self._s.execute(stmt)
        row = result.fetchone()
        if row is None:
            # INSERT ... ON CONFLICT DO UPDATE ... RETURNING always yields a row, so this
            # cannot happen while the statement above is what it looks like. It is checked
            # rather than assumed because the alternative - the old code guarded `row` for
            # None on the next line and then dereferenced it anyway - is to hand the caller
            # a None model that reads as a successfully stored disclosure.
            raise RuntimeError(
                f"upsert of KAP disclosure {dto.kap_disclosure_id} returned no row"
            )

        created = row.ingested_at == row.updated_at
        model = await self._s.get(KapDisclosure, row.id)
        if model is None:
            raise RuntimeError(f"KAP disclosure {row.id} vanished between upsert and read")
        return model, created

    async def upsert_transactions(
        self, disclosure_id: int, txs: list[KapInsiderTxDTO]
    ) -> UpsertResult:
        if not txs:
            return UpsertResult(inserted=0, skipped=0)

        inserted = 0
        skipped = 0
        for tx in txs:
            stmt = (
                insert(KapInsiderTransaction)
                .values(
                    disclosure_id=disclosure_id,
                    insider_name=tx.insider_name,
                    insider_role=tx.insider_role,
                    relation_type=tx.relation_type,
                    is_legal_entity=tx.is_legal_entity,
                    ticker=tx.ticker,
                    transaction_date=tx.transaction_date,
                    transaction_type=tx.transaction_type,
                    share_count=tx.share_count,
                    price_try=tx.price_try,
                    total_value_try=tx.total_value_try,
                    currency=tx.currency,
                    post_tx_share_count=tx.post_tx_share_count,
                    post_tx_ownership_pct=tx.post_tx_ownership_pct,
                    transaction_venue=tx.transaction_venue,
                    notes=tx.notes,
                    natural_key_hash=tx.natural_key_hash,
                )
                .on_conflict_do_nothing(constraint="uq_insider_tx")
            )
            result = await self._s.execute(stmt)
            # rowcount lives on CursorResult, which is what execute() returns for a DML
            # statement; Result is the wider declared type and does not carry it.
            if cast("CursorResult[Any]", result).rowcount == 1:
                inserted += 1
            else:
                skipped += 1

        return UpsertResult(inserted=inserted, skipped=skipped)

    async def disclosure_exists(self, kap_disclosure_id: str) -> bool:
        stmt = select(KapDisclosure.id).where(
            KapDisclosure.kap_disclosure_id == kap_disclosure_id
        )
        result = await self._s.execute(stmt)
        return result.scalar() is not None

    async def create_scraper_run(
        self, scraper_name: str, metadata: dict | None = None
    ) -> ScraperRun:
        run = ScraperRun(scraper_name=scraper_name, status="RUNNING", metadata_=metadata)
        self._s.add(run)
        await self._s.flush()
        await self._s.refresh(run)
        return run

    async def finish_scraper_run(
        self,
        run: ScraperRun,
        *,
        status: str,
        records_seen: int = 0,
        records_inserted: int = 0,
        records_updated: int = 0,
        records_skipped: int = 0,
        error_message: str | None = None,
    ) -> None:
        run.status = status
        run.finished_at = datetime.now(timezone.utc)
        run.records_seen = records_seen
        run.records_inserted = records_inserted
        run.records_updated = records_updated
        run.records_skipped = records_skipped
        run.error_message = error_message
        self._s.add(run)


class GraphRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def upsert_management_roles(
        self,
        ticker: str,
        members: list[BoardMemberDTO],
    ) -> UpsertResult:
        """Replace all source='KAP_YONETIM' roles for this company, then insert fresh.

        DELETE+INSERT is idempotent even when valid_from is NULL (PostgreSQL UNIQUE
        constraints treat NULL as distinct, so ON CONFLICT never fires for NULL
        valid_from).

        When a KAP_YONETIM role is inserted for a person already present in
        KAP_INSIDER_TX for the same company, the KAP_INSIDER_TX row is closed with
        valid_until=today.  This prevents the board_interlocks materialized view from
        generating duplicate (person_id, company_a, company_b) rows - which would
        violate its unique index - when the same person has two active roles at one
        company from different sources.
        """
        company_result = await self._s.execute(
            select(Company.id).where(Company.ticker == ticker)
        )
        company_id = company_result.scalar()
        if company_id is None:
            _log.warning("company_not_found", ticker=ticker)
            return UpsertResult(inserted=0, skipped=0)

        # Full replacement of KAP_YONETIM source rows for this company
        await self._s.execute(
            delete(PersonCompanyRole)
            .where(PersonCompanyRole.company_id == company_id)
            .where(PersonCompanyRole.source == "KAP_YONETIM")
        )

        if not members:
            return UpsertResult(inserted=0, skipped=0)

        today = date_type.today()
        inserted = 0
        for member in members:
            name_norm = normalize_name(member.full_name)
            await self._s.execute(
                insert(Person)
                .values(full_name=member.full_name, name_normalized=name_norm)
                .on_conflict_do_nothing(constraint="uq_person_name")
            )

            person_id_result = await self._s.execute(
                select(Person.id).where(Person.name_normalized == name_norm)
            )
            person_id = person_id_result.scalar()
            if person_id is None:
                continue

            # Close any active KAP_INSIDER_TX role for this person+company so the
            # materialized view does not produce duplicate (person_id, company_a, company_b)
            # rows when one person has multiple valid_until=NULL rows at the same company.
            await self._s.execute(
                update(PersonCompanyRole)
                .where(PersonCompanyRole.person_id == person_id)
                .where(PersonCompanyRole.company_id == company_id)
                .where(PersonCompanyRole.source == "KAP_INSIDER_TX")
                .where(PersonCompanyRole.valid_until.is_(None))
                .values(valid_until=today)
            )

            await self._s.execute(
                insert(PersonCompanyRole).values(
                    person_id=person_id,
                    company_id=company_id,
                    role=member.role,
                    role_type=member.role_type,
                    is_independent=member.is_independent,
                    source="KAP_YONETIM",
                    valid_from=member.valid_from,
                    valid_until=None,
                )
            )
            inserted += 1

        return UpsertResult(inserted=inserted, skipped=0)
