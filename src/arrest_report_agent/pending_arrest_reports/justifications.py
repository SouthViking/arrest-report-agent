"""Durable reminder pauses, with one historical row per authorization."""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime

import sqlalchemy as sa

from arrest_report_agent.infrastructure.postgres import PostgresClient
from arrest_report_agent.infrastructure.postgres.schema import pending_arrest_justifications as table

from .models import PendingReportJustification


class ArrestJustificationRepository:
    def __init__(self, client: PostgresClient) -> None:
        self.client = client

    @staticmethod
    def _scope(guild_id: int, faction_id: int) -> sa.ColumnElement[bool]:
        return sa.and_(table.c.guild_id == guild_id, table.c.faction_id == faction_id)

    async def register(
        self, *, guild_id: int, faction_id: int, report_id: int, officer_name: str,
        reason: str, authorized_by: int, interaction_id: int, now: datetime,
        expires_at: datetime | None,
    ) -> None:
        reason = reason.strip()
        if not 5 <= len(reason) <= 500:
            raise ValueError("El motivo debe tener entre 5 y 500 caracteres.")
        if expires_at is not None and expires_at <= now:
            raise ValueError("El vencimiento debe estar en el futuro.")
        async with self.client.transaction() as tx:
            # Serialize register/remove for this report, including the first insert.
            await tx.execute(sa.select(sa.func.pg_advisory_xact_lock(sa.func.hashtextextended(
                f"arrest-justification:{guild_id}:{faction_id}:{report_id}", 0,
            ))))
            if (await tx.execute(sa.select(table.c.interaction_id).where(
                table.c.interaction_id == interaction_id,
            ))).first() is not None:
                return
            await tx.execute(sa.update(table).where(
                self._scope(guild_id, faction_id), table.c.report_id == report_id,
                table.c.ended_at.is_(None),
            ).values(ended_at=now, ended_by=authorized_by, end_reason="replaced",
                     end_interaction_id=interaction_id))
            await tx.execute(table.insert().values(
                interaction_id=interaction_id, guild_id=guild_id, faction_id=faction_id,
                report_id=report_id, officer_name=officer_name, reason=reason,
                authorized_by=authorized_by, created_at=now, expires_at=expires_at,
            ))

    async def remove(
        self, *, guild_id: int, faction_id: int, report_id: int, user_id: int,
        interaction_id: int, now: datetime,
    ) -> bool:
        async with self.client.transaction() as tx:
            await tx.execute(sa.select(sa.func.pg_advisory_xact_lock(sa.func.hashtextextended(
                f"arrest-justification:{guild_id}:{faction_id}:{report_id}", 0,
            ))))
            if (await tx.execute(sa.select(table.c.interaction_id).where(
                table.c.end_interaction_id == interaction_id, table.c.end_reason == "removed",
                self._scope(guild_id, faction_id), table.c.report_id == report_id,
            ))).first() is not None:
                return True
            result = await tx.execute(sa.update(table).where(
                self._scope(guild_id, faction_id), table.c.report_id == report_id,
                table.c.ended_at.is_(None),
            ).values(ended_at=now, ended_by=user_id, end_reason="removed",
                     end_interaction_id=interaction_id).returning(table.c.interaction_id))
            return result.first() is not None

    async def for_scan(
        self, *, guild_id: int, faction_id: int, observed_ids: Collection[int],
        pending_ids: Collection[int], now: datetime,
    ) -> dict[int, PendingReportJustification]:
        """Close expired/observed completed cases; never infer completion from absence."""

        scope = self._scope(guild_id, faction_id)
        async with self.client.transaction() as tx:
            await tx.execute(sa.update(table).where(
                scope, table.c.ended_at.is_(None), table.c.expires_at <= now,
            ).values(ended_at=table.c.expires_at, end_reason="expired"))
            completed = set(observed_ids) - set(pending_ids)
            if completed:
                await tx.execute(sa.update(table).where(
                    scope, table.c.ended_at.is_(None), table.c.report_id.in_(completed),
                ).values(ended_at=now, end_reason="completed"))
            rows = (await tx.execute(sa.select(table).where(
                scope, table.c.report_id.in_(pending_ids), table.c.ended_at.is_(None),
                sa.or_(table.c.expires_at.is_(None), table.c.expires_at > now),
            ))).mappings()
            return {int(row["report_id"]): PendingReportJustification(
                reason=row["reason"], authorized_by=row["authorized_by"],
                created_at=row["created_at"], expires_at=row["expires_at"],
            ) for row in rows}
