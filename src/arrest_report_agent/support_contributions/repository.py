"""Durable, idempotent storage for voluntary contributions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from arrest_report_agent.infrastructure.postgres import PostgresClient
from arrest_report_agent.infrastructure.postgres.schema import (
    support_contributions,
    support_exchange_rates,
    support_webhook_events,
    supporter_profiles,
    supporter_provider_links,
)

from .models import (
    ContributionEvent,
    ContributionNormalizationStatus,
    ContributionStatus,
    ContributionToNormalize,
    ExchangeRate,
    SupporterProfile,
)

USD = "USD"
IDENTITY_RATE_SOURCE = "identity"


@dataclass(frozen=True, slots=True)
class ContributionIngestResult:
    event_inserted: bool
    contribution_updated: bool


class SupportContributionRepository:
    """Persist contribution events, normalization state and identity links."""

    def __init__(self, client: PostgresClient) -> None:
        self.client = client

    async def record(self, event: ContributionEvent) -> ContributionIngestResult:
        event_id = uuid.uuid4()
        insert_event = (
            pg_insert(support_webhook_events)
            .values(
                id=event_id,
                provider_event_key=event.provider_event_key,
                provider=event.provider,
                provider_event_id=event.provider_event_id,
                event_type=event.event_kind.value,
                live_mode=event.live_mode,
                attempt=event.attempt,
                provider_contribution_id=event.provider_contribution_id,
                payload_sha256=event.payload_sha256,
                event_timestamp=event.event_timestamp,
                status="processed" if event.live_mode else "ignored",
                ignored_reason=None if event.live_mode else "provider_test_event",
            )
            .on_conflict_do_nothing(index_elements=[support_webhook_events.c.provider_event_key])
            .returning(support_webhook_events.c.id)
        )
        async with self.client.transaction() as connection:
            inserted_id = (await connection.execute(insert_event)).scalar_one_or_none()
            if inserted_id is None:
                return ContributionIngestResult(False, False)
            if not event.live_mode:
                return ContributionIngestResult(True, False)

            supporter_profile_id = await self._linked_profile_id(
                event.provider,
                event.provider_supporter_id,
                connection=connection,
            )
            is_usd = event.original_currency == USD
            normalization_values = {
                "normalized_amount_usd": event.original_amount if is_usd else None,
                "exchange_rate_to_usd": Decimal("1") if is_usd else None,
                "exchange_rate_source": IDENTITY_RATE_SOURCE if is_usd else None,
                "exchange_rate_date": event.contributed_at.date() if is_usd else None,
                "normalization_status": (
                    ContributionNormalizationStatus.COMPLETED.value
                    if is_usd
                    else ContributionNormalizationStatus.PENDING.value
                ),
                "normalization_attempts": 0,
                "normalization_next_attempt_at": datetime.now(UTC),
                "normalization_last_error": None,
                "normalized_at": datetime.now(UTC) if is_usd else None,
            }
            insert_contribution = pg_insert(support_contributions).values(
                id=uuid.uuid4(),
                provider=event.provider,
                provider_contribution_id=event.provider_contribution_id,
                transaction_id=event.transaction_id,
                provider_supporter_id=event.provider_supporter_id,
                supporter_profile_id=supporter_profile_id,
                supporter_name=event.supporter_name,
                supporter_name_type=event.supporter_name_type,
                original_amount=event.original_amount,
                original_currency=event.original_currency,
                coffee_count=event.coffee_count,
                coffee_price=event.coffee_price,
                total_amount_charged=event.total_amount_charged,
                application_fee=event.application_fee,
                status=event.status.value,
                contributed_at=event.contributed_at,
                refunded_at=event.refunded_at,
                **normalization_values,
            )
            excluded = insert_contribution.excluded
            same_value = sa.and_(
                support_contributions.c.original_amount == excluded.original_amount,
                support_contributions.c.original_currency == excluded.original_currency,
                support_contributions.c.contributed_at == excluded.contributed_at,
            )
            keep_refunded = sa.case(
                (
                    support_contributions.c.status == ContributionStatus.REFUNDED.value,
                    ContributionStatus.REFUNDED.value,
                ),
                else_=excluded.status,
            )
            normalized_fields = {
                column: sa.case(
                    (same_value, support_contributions.c[column]),
                    else_=getattr(excluded, column),
                )
                for column in (
                    "normalized_amount_usd",
                    "exchange_rate_to_usd",
                    "exchange_rate_source",
                    "exchange_rate_date",
                    "normalization_status",
                    "normalization_attempts",
                    "normalization_next_attempt_at",
                    "normalization_last_error",
                    "normalized_at",
                )
            }
            upsert = insert_contribution.on_conflict_do_update(
                constraint="uq_support_contribution_provider_id",
                set_={
                    "transaction_id": excluded.transaction_id,
                    "provider_supporter_id": excluded.provider_supporter_id,
                    "supporter_profile_id": sa.func.coalesce(
                        excluded.supporter_profile_id,
                        support_contributions.c.supporter_profile_id,
                    ),
                    "supporter_name": excluded.supporter_name,
                    "supporter_name_type": excluded.supporter_name_type,
                    "original_amount": excluded.original_amount,
                    "original_currency": excluded.original_currency,
                    "coffee_count": excluded.coffee_count,
                    "coffee_price": excluded.coffee_price,
                    "total_amount_charged": excluded.total_amount_charged,
                    "application_fee": excluded.application_fee,
                    "status": keep_refunded,
                    "contributed_at": excluded.contributed_at,
                    "refunded_at": sa.func.coalesce(
                        excluded.refunded_at,
                        support_contributions.c.refunded_at,
                    ),
                    "updated_at": sa.func.now(),
                    **normalized_fields,
                },
            )
            await connection.execute(upsert)
        return ContributionIngestResult(True, True)

    async def due_for_normalization(
        self,
        *,
        limit: int = 50,
    ) -> tuple[ContributionToNormalize, ...]:
        rows = await self.client.fetch_all(
            sa.select(
                support_contributions.c.id,
                support_contributions.c.provider,
                support_contributions.c.provider_contribution_id,
                support_contributions.c.original_amount,
                support_contributions.c.original_currency,
                support_contributions.c.contributed_at,
                support_contributions.c.normalization_attempts,
            )
            .where(
                support_contributions.c.normalization_status.in_(
                    (
                        ContributionNormalizationStatus.PENDING.value,
                        ContributionNormalizationStatus.RETRY.value,
                    )
                ),
                support_contributions.c.normalization_next_attempt_at <= sa.func.now(),
            )
            .order_by(support_contributions.c.contributed_at)
            .limit(limit)
        )
        return tuple(
            ContributionToNormalize(
                id=UUID(str(row["id"])),
                provider=str(row["provider"]),
                provider_contribution_id=str(row["provider_contribution_id"]),
                original_amount=Decimal(row["original_amount"]),
                original_currency=str(row["original_currency"]),
                contributed_at=row["contributed_at"],
                attempts=int(row["normalization_attempts"]),
            )
            for row in rows
        )

    async def exchange_rate(
        self,
        *,
        source: str,
        requested_date: date,
        base_currency: str,
        quote_currency: str,
    ) -> ExchangeRate | None:
        row = await self.client.fetch_one(
            sa.select(support_exchange_rates).where(
                support_exchange_rates.c.source == source,
                support_exchange_rates.c.requested_date == requested_date,
                support_exchange_rates.c.base_currency == base_currency,
                support_exchange_rates.c.quote_currency == quote_currency,
            )
        )
        return self._exchange_rate_from_row(row) if row is not None else None

    async def save_exchange_rate(self, rate: ExchangeRate) -> ExchangeRate:
        statement = (
            pg_insert(support_exchange_rates)
            .values(
                id=uuid.uuid4(),
                source=rate.source,
                requested_date=rate.requested_date,
                effective_date=rate.effective_date,
                base_currency=rate.base_currency,
                quote_currency=rate.quote_currency,
                rate=rate.rate,
            )
            .on_conflict_do_nothing(constraint="uq_support_exchange_rate_lookup")
        )
        async with self.client.transaction() as connection:
            await connection.execute(statement)
            row = await self.client.fetch_one(
                sa.select(support_exchange_rates).where(
                    support_exchange_rates.c.source == rate.source,
                    support_exchange_rates.c.requested_date == rate.requested_date,
                    support_exchange_rates.c.base_currency == rate.base_currency,
                    support_exchange_rates.c.quote_currency == rate.quote_currency,
                ),
                connection=connection,
            )
        assert row is not None
        return self._exchange_rate_from_row(row)

    async def mark_normalized(
        self,
        contribution_id: UUID,
        *,
        normalized_amount_usd: Decimal,
        rate: ExchangeRate,
    ) -> None:
        now = datetime.now(UTC)
        await self.client.execute(
            support_contributions.update()
            .where(support_contributions.c.id == contribution_id)
            .values(
                normalized_amount_usd=normalized_amount_usd,
                exchange_rate_to_usd=rate.rate,
                exchange_rate_source=rate.source,
                exchange_rate_date=rate.effective_date,
                normalization_status=ContributionNormalizationStatus.COMPLETED.value,
                normalization_last_error=None,
                normalized_at=now,
                updated_at=now,
            )
        )

    async def mark_normalization_failed(
        self,
        contribution_id: UUID,
        *,
        attempts: int,
        error: str,
        retryable: bool,
        max_attempts: int = 8,
    ) -> None:
        next_attempt = attempts + 1
        terminal = not retryable or next_attempt >= max_attempts
        backoff_seconds = min(15 * 3 ** max(next_attempt - 1, 0), 3600)
        now = datetime.now(UTC)
        await self.client.execute(
            support_contributions.update()
            .where(support_contributions.c.id == contribution_id)
            .values(
                normalization_status=(
                    ContributionNormalizationStatus.FAILED.value
                    if terminal
                    else ContributionNormalizationStatus.RETRY.value
                ),
                normalization_attempts=next_attempt,
                normalization_next_attempt_at=now + timedelta(seconds=backoff_seconds),
                normalization_last_error=error[:500],
                updated_at=now,
            )
        )

    async def upsert_supporter_profile(
        self,
        discord_user_id: int,
        *,
        display_name: str | None = None,
        mention_enabled: bool = True,
    ) -> SupporterProfile:
        if discord_user_id <= 0:
            raise ValueError("discord_user_id must be positive")
        clean_name = display_name.strip()[:200] if display_name and display_name.strip() else None
        statement = (
            pg_insert(supporter_profiles)
            .values(
                id=uuid.uuid4(),
                discord_user_id=discord_user_id,
                display_name=clean_name,
                mention_enabled=mention_enabled,
            )
            .on_conflict_do_update(
                index_elements=[supporter_profiles.c.discord_user_id],
                set_={
                    "display_name": sa.func.coalesce(
                        clean_name,
                        supporter_profiles.c.display_name,
                    ),
                    "mention_enabled": mention_enabled,
                    "updated_at": sa.func.now(),
                },
            )
            .returning(supporter_profiles)
        )
        row = (await self.client.execute(statement)).mappings().one()
        return self._profile_from_row(row)

    async def link_provider_supporter(
        self,
        profile_id: UUID,
        *,
        provider: str,
        provider_supporter_id: str,
    ) -> None:
        clean_provider = provider.strip().lower()
        clean_supporter_id = provider_supporter_id.strip()
        if not clean_provider or not clean_supporter_id:
            raise ValueError("provider and provider_supporter_id are required")
        statement = (
            pg_insert(supporter_provider_links)
            .values(
                id=uuid.uuid4(),
                profile_id=profile_id,
                provider=clean_provider,
                provider_supporter_id=clean_supporter_id,
            )
            .on_conflict_do_update(
                constraint="uq_supporter_provider_identity",
                set_={"profile_id": profile_id, "updated_at": sa.func.now()},
            )
        )
        async with self.client.transaction() as connection:
            await connection.execute(statement)
            await connection.execute(
                support_contributions.update()
                .where(
                    support_contributions.c.provider == clean_provider,
                    support_contributions.c.provider_supporter_id == clean_supporter_id,
                )
                .values(supporter_profile_id=profile_id, updated_at=sa.func.now())
            )

    async def attribute_contribution(
        self,
        contribution_id: UUID,
        *,
        profile_id: UUID | None,
    ) -> bool:
        result = await self.client.execute(
            support_contributions.update()
            .where(support_contributions.c.id == contribution_id)
            .values(supporter_profile_id=profile_id, updated_at=sa.func.now())
        )
        return bool(result.rowcount)

    async def _linked_profile_id(
        self,
        provider: str,
        provider_supporter_id: str | None,
        *,
        connection: AsyncConnection,
    ) -> UUID | None:
        if provider_supporter_id is None:
            return None
        row = await self.client.fetch_one(
            sa.select(supporter_provider_links.c.profile_id).where(
                supporter_provider_links.c.provider == provider,
                supporter_provider_links.c.provider_supporter_id == provider_supporter_id,
            ),
            connection=connection,
        )
        return UUID(str(row["profile_id"])) if row is not None else None

    @staticmethod
    def _exchange_rate_from_row(row: sa.RowMapping) -> ExchangeRate:
        return ExchangeRate(
            source=str(row["source"]),
            requested_date=row["requested_date"],
            effective_date=row["effective_date"],
            base_currency=str(row["base_currency"]),
            quote_currency=str(row["quote_currency"]),
            rate=Decimal(row["rate"]),
        )

    @staticmethod
    def _profile_from_row(row: sa.RowMapping) -> SupporterProfile:
        return SupporterProfile(
            id=UUID(str(row["id"])),
            discord_user_id=int(row["discord_user_id"]),
            display_name=str(row["display_name"]) if row["display_name"] is not None else None,
            mention_enabled=bool(row["mention_enabled"]),
        )
