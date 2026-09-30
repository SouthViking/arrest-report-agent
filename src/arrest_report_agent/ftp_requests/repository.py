"""PostgreSQL persistence for FTP assistance requests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from arrest_report_agent.infrastructure.postgres import PostgresClient
from arrest_report_agent.infrastructure.postgres.schema import (
    discord_guilds,
    ftp_request_panels,
    ftp_training_requests,
)

from .models import (
    FtpInstructorLevel,
    FtpRequestPanel,
    FtpRequestStatus,
    FtpTrainingPhase,
    FtpTrainingRequest,
)


class FtpRequestRepository:
    def __init__(self, client: PostgresClient) -> None:
        self.client = client

    async def create_publishing(
        self,
        *,
        guild_id: int,
        channel_id: int,
        requester_user_id: int,
        training_phase: FtpTrainingPhase,
        requested_instructor_level: FtpInstructorLevel,
        scheduled_for: datetime,
        reason: str = "",
    ) -> FtpTrainingRequest:
        normalized_reason = reason.strip()
        if len(normalized_reason) > 500:
            raise ValueError("FTP request details cannot exceed 500 characters")
        if scheduled_for.tzinfo is None:
            raise ValueError("FTP request schedule must include timezone information")
        normalized_schedule = scheduled_for.astimezone(UTC)
        async with self.client.transaction() as tx:
            guild = await tx.scalar(
                sa.select(discord_guilds.c.discord_id).where(
                    discord_guilds.c.discord_id == guild_id
                )
            )
            if guild is None:
                raise LookupError(f"Guild {guild_id} is not configured")
            request_id = uuid.uuid4()
            await tx.execute(
                ftp_training_requests.insert().values(
                    id=request_id,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    requester_user_id=requester_user_id,
                    reason=normalized_reason,
                    training_phase=training_phase.value,
                    requested_instructor_level=requested_instructor_level.value,
                    scheduled_for=normalized_schedule,
                    status=FtpRequestStatus.PUBLISHING.value,
                )
            )
            return await self._get(request_id, tx)

    async def get_panel(self, guild_id: int) -> FtpRequestPanel | None:
        row = await self.client.fetch_one(
            sa.select(ftp_request_panels).where(ftp_request_panels.c.guild_id == guild_id)
        )
        return None if row is None else FtpRequestPanel.model_validate(dict(row))

    async def save_panel(
        self,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
    ) -> FtpRequestPanel:
        statement = insert(ftp_request_panels).values(
            guild_id=guild_id,
            channel_id=channel_id,
            message_id=message_id,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[ftp_request_panels.c.guild_id],
            set_={
                "channel_id": channel_id,
                "message_id": message_id,
                "updated_at": datetime.now(UTC),
            },
        )
        await self.client.execute(statement)
        panel = await self.get_panel(guild_id)
        if panel is None:
            raise LookupError(f"FTP request panel for guild {guild_id} was not persisted")
        return panel

    async def publish(self, request_id: UUID, message_id: int) -> FtpTrainingRequest:
        async with self.client.transaction() as tx:
            result = await tx.execute(
                ftp_training_requests.update()
                .where(
                    ftp_training_requests.c.id == request_id,
                    ftp_training_requests.c.status == FtpRequestStatus.PUBLISHING.value,
                )
                .values(
                    message_id=message_id,
                    status=FtpRequestStatus.PENDING.value,
                    updated_at=datetime.now(UTC),
                )
            )
            if not result.rowcount:
                raise LookupError("FTP request is no longer publishing")
            return await self._get(request_id, tx)

    async def mark_failed(self, request_id: UUID) -> None:
        now = datetime.now(UTC)
        await self.client.execute(
            ftp_training_requests.update()
            .where(
                ftp_training_requests.c.id == request_id,
                ftp_training_requests.c.status == FtpRequestStatus.PUBLISHING.value,
            )
            .values(
                status=FtpRequestStatus.FAILED.value,
                updated_at=now,
                resolved_at=now,
            )
        )

    async def get_by_message(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
    ) -> FtpTrainingRequest | None:
        row = await self.client.fetch_one(
            sa.select(ftp_training_requests).where(
                ftp_training_requests.c.guild_id == guild_id,
                ftp_training_requests.c.channel_id == channel_id,
                ftp_training_requests.c.message_id == message_id,
            )
        )
        return None if row is None else self._from_row(row)

    async def list_pending(
        self,
        guild_id: int,
        *,
        offset: int,
        limit: int,
    ) -> tuple[tuple[FtpTrainingRequest, ...], int]:
        where = (
            ftp_training_requests.c.guild_id == guild_id,
            ftp_training_requests.c.status == FtpRequestStatus.PENDING.value,
        )
        count_row = await self.client.fetch_one(
            sa.select(sa.func.count().label("count")).where(*where)
        )
        total = int(count_row["count"] if count_row is not None else 0)
        rows = await self.client.fetch_all(
            sa.select(ftp_training_requests)
            .where(*where)
            .order_by(
                ftp_training_requests.c.scheduled_for.asc().nulls_last(),
                ftp_training_requests.c.created_at.asc(),
            )
            .offset(offset)
            .limit(limit)
        )
        return tuple(self._from_row(row) for row in rows), total

    async def claim(
        self,
        request_id: UUID,
        *,
        actor_user_id: int,
    ) -> tuple[FtpTrainingRequest, bool]:
        return await self._transition(
            request_id,
            status=FtpRequestStatus.CLAIMED,
            actor_user_id=actor_user_id,
            claimed_by_user_id=actor_user_id,
        )

    async def expire_pending(
        self, guild_id: int, *, cutoff: datetime, now: datetime, limit: int = 50,
    ) -> tuple[FtpTrainingRequest, ...]:
        """Atomically cancel only still-pending rows; never race a successful claim.

        Old, unstructured requests without a schedule use their creation date.
        The retry marker commits with the cancellation before any Discord calls.
        """
        if cutoff.tzinfo is None or now.tzinfo is None or not 1 <= limit <= 100:
            raise ValueError("Expiry requires aware timestamps and a bounded batch")
        table = ftp_training_requests
        due_at = sa.func.coalesce(table.c.scheduled_for, table.c.created_at)
        candidates = (
            sa.select(table.c.id)
            .where(table.c.guild_id == guild_id, table.c.status == FtpRequestStatus.PENDING.value,
                   due_at <= cutoff)
            .order_by(due_at, table.c.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .cte("ftp_expiry_candidates")
            .prefix_with("MATERIALIZED")
        )
        async with self.client.transaction() as tx:
            result = await tx.execute(
                table.update()
                .where(table.c.id.in_(sa.select(candidates.c.id)),
                       table.c.status == FtpRequestStatus.PENDING.value)
                .values(status=FtpRequestStatus.CANCELLED.value, resolved_at=now, updated_at=now,
                        resolved_by_user_id=None, auto_cancelled_at=now, expiry_sync_due_at=now)
                .returning(table)
            )
            return tuple(self._from_row(row) for row in result.mappings())

    async def list_expiry_sync(
        self, guild_id: int, *, now: datetime, limit: int = 50,
    ) -> tuple[FtpTrainingRequest, ...]:
        rows = await self.client.fetch_all(
            sa.select(ftp_training_requests)
            .where(ftp_training_requests.c.guild_id == guild_id,
                   ftp_training_requests.c.expiry_sync_due_at <= now)
            .order_by(ftp_training_requests.c.expiry_sync_due_at, ftp_training_requests.c.id)
            .limit(limit)
        )
        return tuple(self._from_row(row) for row in rows)

    async def set_expiry_sync_due(self, request: FtpTrainingRequest, due_at: datetime | None) -> None:
        """Acknowledge an edit or defer its retry without changing the cancellation."""
        await self.client.execute(
            ftp_training_requests.update()
            .where(ftp_training_requests.c.id == request.id,
                   ftp_training_requests.c.guild_id == request.guild_id,
                   ftp_training_requests.c.auto_cancelled_at == request.auto_cancelled_at,
                   ftp_training_requests.c.expiry_sync_due_at == request.expiry_sync_due_at)
            .values(expiry_sync_due_at=due_at)
        )

    async def cancel(
        self,
        request_id: UUID,
        *,
        actor_user_id: int,
    ) -> tuple[FtpTrainingRequest, bool]:
        return await self._transition(
            request_id,
            status=FtpRequestStatus.CANCELLED,
            actor_user_id=actor_user_id,
            claimed_by_user_id=None,
        )

    async def restore_pending(
        self,
        request_id: UUID,
        *,
        expected_status: FtpRequestStatus,
    ) -> FtpTrainingRequest:
        if expected_status not in {FtpRequestStatus.CLAIMED, FtpRequestStatus.CANCELLED}:
            raise ValueError("Expected status must be a public terminal state")
        async with self.client.transaction() as tx:
            await tx.execute(
                ftp_training_requests.update()
                .where(
                    ftp_training_requests.c.id == request_id,
                    ftp_training_requests.c.status == expected_status.value,
                    ftp_training_requests.c.auto_cancelled_at.is_(None),
                )
                .values(
                    status=FtpRequestStatus.PENDING.value,
                    claimed_by_user_id=None,
                    resolved_by_user_id=None,
                    resolved_at=None,
                    updated_at=datetime.now(UTC),
                )
            )
            return await self._get(request_id, tx)

    async def _transition(
        self,
        request_id: UUID,
        *,
        status: FtpRequestStatus,
        actor_user_id: int,
        claimed_by_user_id: int | None,
    ) -> tuple[FtpTrainingRequest, bool]:
        now = datetime.now(UTC)
        async with self.client.transaction() as tx:
            result = await tx.execute(
                ftp_training_requests.update()
                .where(
                    ftp_training_requests.c.id == request_id,
                    ftp_training_requests.c.status == FtpRequestStatus.PENDING.value,
                )
                .values(
                    status=status.value,
                    claimed_by_user_id=claimed_by_user_id,
                    resolved_by_user_id=actor_user_id,
                    resolved_at=now,
                    updated_at=now,
                )
            )
            return await self._get(request_id, tx), bool(result.rowcount)

    async def _get(self, request_id: UUID, tx: AsyncConnection) -> FtpTrainingRequest:
        result = await tx.execute(
            sa.select(ftp_training_requests).where(ftp_training_requests.c.id == request_id)
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise LookupError(f"FTP request {request_id} does not exist")
        return self._from_row(row)

    @staticmethod
    def _from_row(row: sa.RowMapping) -> FtpTrainingRequest:
        return FtpTrainingRequest.model_validate(dict(row))
