"""Transactional quota storage for limited report-owner agent runs."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from arrest_report_agent.infrastructure.postgres import PostgresClient
from arrest_report_agent.infrastructure.postgres.schema import (
    report_owner_agent_runs,
    report_owner_round_grants,
)

from .models import (
    OwnerRoundGrant,
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
            await self._lock_owner_report(connection, guild_id, root_message_id, owner_user_id)
            existing = cast(
                UUID | None,
                (
                    await connection.execute(
                        sa.select(report_owner_agent_runs.c.id).where(
                            report_owner_agent_runs.c.trigger_message_id == trigger_message_id
                        )
                    )
                ).scalar_one_or_none(),
            )
            used = await self._used_rounds(connection, guild_id, root_message_id, owner_user_id)
            max_rounds += await self._extra_rounds(connection, guild_id, root_message_id, owner_user_id)
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

    async def grant_extra_rounds(
        self,
        *,
        guild_id: int,
        root_message_id: int,
        owner_user_id: int,
        granted_by_user_id: int,
        interaction_id: int,
        amount: int,
        base_max_rounds: int,
    ) -> OwnerRoundGrant:
        """Add rounds to one owner/report without rewriting completed run history."""

        if not 1 <= amount <= 10:
            raise ValueError("Extra rounds must be between 1 and 10")
        if not 1 <= base_max_rounds <= 20:
            raise ValueError("Report-owner base rounds must be between 1 and 20")
        async with self.client.transaction() as connection:
            await self._lock_owner_report(connection, guild_id, root_message_id, owner_user_id)
            existing = (
                await connection.execute(
                    sa.select(report_owner_round_grants).where(
                        report_owner_round_grants.c.interaction_id == interaction_id
                    )
                )
            ).mappings().one_or_none()
            if existing is not None and (
                int(existing["guild_id"]) != guild_id
                or int(existing["root_message_id"]) != root_message_id
                or int(existing["owner_user_id"]) != owner_user_id
                or int(existing["granted_by_user_id"]) != granted_by_user_id
                or int(existing["amount"]) != amount
            ):
                raise ValueError("Interaction ID belongs to a different grant")
            used = await self._used_rounds(connection, guild_id, root_message_id, owner_user_id)
            extra = await self._extra_rounds(connection, guild_id, root_message_id, owner_user_id)
            if existing is not None:
                return OwnerRoundGrant(
                    added_rounds=0,
                    used_rounds=used,
                    max_rounds=base_max_rounds + extra,
                    duplicate=True,
                )
            await connection.execute(
                report_owner_round_grants.insert().values(
                    id=uuid.uuid4(),
                    guild_id=guild_id,
                    root_message_id=root_message_id,
                    owner_user_id=owner_user_id,
                    granted_by_user_id=granted_by_user_id,
                    interaction_id=interaction_id,
                    amount=amount,
                )
            )
            return OwnerRoundGrant(
                added_rounds=amount,
                used_rounds=used,
                max_rounds=base_max_rounds + extra + amount,
            )

    @staticmethod
    async def _lock_owner_report(
        connection: AsyncConnection,
        guild_id: int,
        root_message_id: int,
        owner_user_id: int,
    ) -> None:
        await connection.execute(
            sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"report-owner:{guild_id}:{root_message_id}:{owner_user_id}"},
        )

    @staticmethod
    async def _used_rounds(
        connection: AsyncConnection,
        guild_id: int,
        root_message_id: int,
        owner_user_id: int,
    ) -> int:
        return int(
            await connection.scalar(
                sa.select(sa.func.count(report_owner_agent_runs.c.id)).where(
                    report_owner_agent_runs.c.guild_id == guild_id,
                    report_owner_agent_runs.c.root_message_id == root_message_id,
                    report_owner_agent_runs.c.owner_user_id == owner_user_id,
                )
            )
            or 0
        )

    @staticmethod
    async def _extra_rounds(
        connection: AsyncConnection,
        guild_id: int,
        root_message_id: int,
        owner_user_id: int,
    ) -> int:
        return int(
            await connection.scalar(
                sa.select(sa.func.sum(report_owner_round_grants.c.amount)).where(
                    report_owner_round_grants.c.guild_id == guild_id,
                    report_owner_round_grants.c.root_message_id == root_message_id,
                    report_owner_round_grants.c.owner_user_id == owner_user_id,
                )
            )
            or 0
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
