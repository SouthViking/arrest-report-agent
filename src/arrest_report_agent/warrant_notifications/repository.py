"""Atomic PostgreSQL transitions for warrant notifications."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from arrest_report_agent.infrastructure.postgres import PostgresClient
from arrest_report_agent.infrastructure.postgres.schema import warrant_notifications

from .models import WarrantNotificationRecord, WarrantNotificationStatus


class WarrantNotificationRepository:
    def __init__(self, client: PostgresClient) -> None:
        self.client = client

    async def create_publishing(
        self,
        *,
        guild_id: int,
        interaction_id: int,
        channel_id: int,
        requester_user_id: int,
        report_url: str,
        warrant_id: int,
        warrant_type: str,
    ) -> tuple[WarrantNotificationRecord, bool]:
        record_id = uuid.uuid4()
        statement = (
            insert(warrant_notifications)
            .values(
                id=record_id,
                guild_id=guild_id,
                interaction_id=interaction_id,
                channel_id=channel_id,
                requester_user_id=requester_user_id,
                report_url=report_url,
                warrant_id=warrant_id,
                warrant_type=warrant_type,
                status=WarrantNotificationStatus.PUBLISHING.value,
            )
            .on_conflict_do_nothing(index_elements=[warrant_notifications.c.interaction_id])
            .returning(warrant_notifications.c.id)
        )
        async with self.client.transaction() as tx:
            inserted = (await tx.execute(statement)).scalar_one_or_none()
            result = await tx.execute(
                sa.select(warrant_notifications).where(
                    warrant_notifications.c.interaction_id == interaction_id
                )
            )
            row = result.mappings().one()
        return self._from_row(row), inserted is not None

    async def publish(self, record_id: UUID, message_id: int) -> WarrantNotificationRecord:
        async with self.client.transaction() as tx:
            result = await tx.execute(
                warrant_notifications.update()
                .where(
                    warrant_notifications.c.id == record_id,
                    warrant_notifications.c.status == WarrantNotificationStatus.PUBLISHING.value,
                )
                .values(
                    message_id=message_id,
                    status=WarrantNotificationStatus.PENDING.value,
                    updated_at=datetime.now(UTC),
                )
                .returning(warrant_notifications)
            )
            row = result.mappings().one_or_none()
        if row is None:
            raise LookupError("Warrant notification is no longer publishing")
        return self._from_row(row)

    async def mark_failed(self, record_id: UUID) -> None:
        await self.client.execute(
            warrant_notifications.update()
            .where(
                warrant_notifications.c.id == record_id,
                warrant_notifications.c.status == WarrantNotificationStatus.PUBLISHING.value,
            )
            .values(status=WarrantNotificationStatus.FAILED.value, updated_at=datetime.now(UTC))
        )

    async def get_by_message(
        self, guild_id: int, channel_id: int, message_id: int
    ) -> WarrantNotificationRecord | None:
        row = await self.client.fetch_one(
            sa.select(warrant_notifications).where(
                warrant_notifications.c.guild_id == guild_id,
                warrant_notifications.c.channel_id == channel_id,
                warrant_notifications.c.message_id == message_id,
            )
        )
        return self._from_row(row) if row is not None else None

    async def resolve(
        self,
        record_id: UUID,
        *,
        status: WarrantNotificationStatus,
        resolver_user_id: int,
        rejection_reason: str | None = None,
    ) -> tuple[WarrantNotificationRecord, bool]:
        if status not in {WarrantNotificationStatus.SIGNED, WarrantNotificationStatus.REJECTED}:
            raise ValueError("A warrant can only be signed or rejected")
        reason = (rejection_reason or "").strip() or None
        if status is WarrantNotificationStatus.SIGNED and reason is not None:
            raise ValueError("A signed warrant cannot have a rejection reason")
        if reason is not None and len(reason) > 500:
            raise ValueError("Rejection reason exceeds 500 characters")
        async with self.client.transaction() as tx:
            result = await tx.execute(
                warrant_notifications.update()
                .where(
                    warrant_notifications.c.id == record_id,
                    warrant_notifications.c.status == WarrantNotificationStatus.PENDING.value,
                )
                .values(
                    status=status.value,
                    resolved_by_user_id=resolver_user_id,
                    rejection_reason=reason,
                    resolved_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
            current = await tx.execute(
                sa.select(warrant_notifications).where(warrant_notifications.c.id == record_id)
            )
            row = current.mappings().one()
        return self._from_row(row), bool(result.rowcount)

    async def mark_result_sent(self, record_id: UUID, result_message_id: int) -> None:
        await self.client.execute(
            warrant_notifications.update()
            .where(
                warrant_notifications.c.id == record_id,
                warrant_notifications.c.result_message_id.is_(None),
            )
            .values(result_message_id=result_message_id, updated_at=datetime.now(UTC))
        )

    async def restore_pending(
        self,
        record_id: UUID,
        *,
        expected_status: WarrantNotificationStatus,
        resolver_user_id: int,
    ) -> bool:
        if not expected_status.resolved:
            raise ValueError("Only a resolution can be rolled back")
        result = await self.client.execute(
            warrant_notifications.update()
            .where(
                warrant_notifications.c.id == record_id,
                warrant_notifications.c.status == expected_status.value,
                warrant_notifications.c.resolved_by_user_id == resolver_user_id,
                warrant_notifications.c.result_message_id.is_(None),
            )
            .values(
                status=WarrantNotificationStatus.PENDING.value,
                resolved_by_user_id=None,
                rejection_reason=None,
                resolved_at=None,
                updated_at=datetime.now(UTC),
            )
        )
        return bool(result.rowcount)

    @staticmethod
    def _from_row(row: sa.RowMapping) -> WarrantNotificationRecord:
        return WarrantNotificationRecord.model_validate(dict(row))
