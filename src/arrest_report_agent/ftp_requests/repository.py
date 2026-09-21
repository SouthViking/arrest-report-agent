"""PostgreSQL persistence for FTP assistance requests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from arrest_report_agent.infrastructure.postgres import PostgresClient
from arrest_report_agent.infrastructure.postgres.schema import (
    discord_guilds,
    ftp_training_requests,
)

from .models import (
    FtpInstructorLevel,
    FtpRequestStatus,
    FtpTrainingPhase,
    FtpTrainingRequest,
)


class ActiveFtpRequestError(ValueError):
    """Raised when the same member already has a pending request."""


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
                sa.select(discord_guilds.c.discord_id)
                .where(discord_guilds.c.discord_id == guild_id)
                .with_for_update()
            )
            if guild is None:
                raise LookupError(f"Guild {guild_id} is not configured")
            # Serialize attempts per member before consulting the partial unique key.
            await tx.execute(
                sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"ftp-request:{guild_id}:{requester_user_id}"},
            )
            active = await tx.scalar(
                sa.select(ftp_training_requests.c.id).where(
                    ftp_training_requests.c.guild_id == guild_id,
                    ftp_training_requests.c.requester_user_id == requester_user_id,
                    ftp_training_requests.c.status.in_(
                        (FtpRequestStatus.PUBLISHING.value, FtpRequestStatus.PENDING.value)
                    ),
                )
            )
            if active is not None:
                raise ActiveFtpRequestError("Member already has a pending FTP request")
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
