"""Transactional quota storage for limited report-owner agent runs."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa

from arrest_report_agent.infrastructure.postgres import PostgresClient
from arrest_report_agent.infrastructure.postgres.schema import report_owner_agent_runs

from .models import (
    OwnerRunReservation,
    OwnerRunReservationResult,
    OwnerRunStatus,
)


class ReportOwnerRunRepository:
    """Reserve report-owner rounds atomically and keep terminal outcomes."""

    def __init__(self, client: PostgresClient) -> None:
        self.client = client

    async def reserve(
        self,
        *,
        guild_id: int,
        root_message_id: int,
        trigger_message_id: int,
        owner_user_id: int,
        thread_id: int | None,
        max_rounds: int,
    ) -> OwnerRunReservation:
        if not 1 <= max_rounds <= 20:
            raise ValueError("Report-owner max rounds must be between 1 and 20")
        async with self.client.transaction() as connection:
            await connection.execute(
                sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"report-owner:{guild_id}:{root_message_id}:{owner_user_id}"},
            )
            existing = (
                await connection.execute(
                    sa.select(report_owner_agent_runs.c.id).where(
                        report_owner_agent_runs.c.trigger_message_id == trigger_message_id
                    )
                )
            ).scalar_one_or_none()
            used = int(
                await connection.scalar(
                    sa.select(sa.func.count(report_owner_agent_runs.c.id)).where(
                        report_owner_agent_runs.c.guild_id == guild_id,
                        report_owner_agent_runs.c.root_message_id == root_message_id,
                        report_owner_agent_runs.c.owner_user_id == owner_user_id,
                    )
                )
                or 0
            )
            if existing is not None:
                return OwnerRunReservation(
                    result=OwnerRunReservationResult.DUPLICATE,
                    used_rounds=used,
                    max_rounds=max_rounds,
                )
            if used >= max_rounds:
                return OwnerRunReservation(
                    result=OwnerRunReservationResult.EXHAUSTED,
                    used_rounds=used,
                    max_rounds=max_rounds,
                )
            run_id = uuid.uuid4()
            await connection.execute(
                report_owner_agent_runs.insert().values(
                    id=run_id,
                    guild_id=guild_id,
                    root_message_id=root_message_id,
                    trigger_message_id=trigger_message_id,
                    owner_user_id=owner_user_id,
                    thread_id=thread_id,
                    status=OwnerRunStatus.STARTED.value,
                )
            )
            return OwnerRunReservation(
                result=OwnerRunReservationResult.RESERVED,
                used_rounds=used + 1,
                max_rounds=max_rounds,
                run_id=run_id,
            )

    async def mark_completed(self, run_id: UUID) -> None:
        await self._finish(run_id, OwnerRunStatus.COMPLETED)

    async def mark_failed(self, run_id: UUID) -> None:
        await self._finish(run_id, OwnerRunStatus.FAILED)

    async def _finish(self, run_id: UUID, status: OwnerRunStatus) -> None:
        now = datetime.now(UTC)
        values: dict[str, object] = {"status": status.value}
        values["completed_at" if status is OwnerRunStatus.COMPLETED else "failed_at"] = now
        await self.client.execute(
            report_owner_agent_runs.update()
            .where(
                report_owner_agent_runs.c.id == run_id,
                report_owner_agent_runs.c.status == OwnerRunStatus.STARTED.value,
            )
            .values(**values)
        )
