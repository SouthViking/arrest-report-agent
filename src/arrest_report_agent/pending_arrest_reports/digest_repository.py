"""Durable schedule, per-channel deliveries and restrained mention history."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from arrest_report_agent.infrastructure.postgres import PostgresClient
from arrest_report_agent.infrastructure.postgres.schema import (
    pending_arrest_digest_deliveries,
    pending_arrest_digest_mentions,
    pending_arrest_digest_state,
)


class PendingArrestDigestRepository:
    def __init__(self, client: PostgresClient) -> None:
        self.client = client

    async def claim(self, guild_id: int, *, now: datetime, interval_hours: int) -> uuid.UUID | None:
        state = pending_arrest_digest_state
        async with self.client.transaction() as connection:
            await self.client.execute(
                insert(state)
                .values(guild_id=guild_id, next_due_at=now + timedelta(hours=interval_hours))
                .on_conflict_do_nothing(index_elements=[state.c.guild_id]),
                connection=connection,
            )
            cycle_id = uuid.uuid4()
            result = await self.client.execute(
                sa.update(state)
                .where(
                    state.c.guild_id == guild_id,
                    state.c.next_due_at <= now,
                    sa.or_(state.c.lease_until.is_(None), state.c.lease_until <= now),
                )
                .values(
                    cycle_id=sa.func.coalesce(state.c.cycle_id, cycle_id),
                    lease_until=now + timedelta(minutes=20),
                )
                .returning(state.c.cycle_id),
                connection=connection,
            )
            row = result.first()
            return row[0] if row is not None else None

    async def finish(self, guild_id: int, cycle_id: uuid.UUID, *, now: datetime, interval_hours: int) -> None:
        state = pending_arrest_digest_state
        await self.client.execute(
            sa.update(state)
            .where(state.c.guild_id == guild_id, state.c.cycle_id == cycle_id)
            .values(
                next_due_at=now + timedelta(hours=interval_hours),
                cycle_id=None,
                lease_until=None,
                last_completed_at=now,
            )
        )

    async def reschedule(self, guild_id: int, *, now: datetime, interval_hours: int) -> None:
        """Apply a new interval without interrupting an in-flight cycle."""

        state = pending_arrest_digest_state
        await self.client.execute(
            sa.update(state)
            .where(state.c.guild_id == guild_id, state.c.cycle_id.is_(None))
            .values(
                next_due_at=sa.func.coalesce(state.c.last_completed_at, now) + timedelta(hours=interval_hours)
            )
        )

    async def retry(self, guild_id: int, cycle_id: uuid.UUID, *, now: datetime) -> None:
        state = pending_arrest_digest_state
        await self.client.execute(
            sa.update(state)
            .where(state.c.guild_id == guild_id, state.c.cycle_id == cycle_id)
            .values(next_due_at=now + timedelta(minutes=5), lease_until=None)
        )

    async def deliveries(self, guild_id: int, cycle_id: uuid.UUID) -> dict[int, tuple[int, bool]]:
        rows = await self.client.fetch_all(
            sa.select(pending_arrest_digest_deliveries).where(
                pending_arrest_digest_deliveries.c.guild_id == guild_id,
                pending_arrest_digest_deliveries.c.cycle_id == cycle_id,
            )
        )
        return {int(row["channel_id"]): (int(row["message_id"]), bool(row["reply_sent"])) for row in rows}

    async def record_delivery(
        self, guild_id: int, cycle_id: uuid.UUID, channel_id: int, message_id: int
    ) -> None:
        table = pending_arrest_digest_deliveries
        await self.client.execute(
            insert(table)
            .values(guild_id=guild_id, cycle_id=cycle_id, channel_id=channel_id, message_id=message_id)
            .on_conflict_do_nothing(index_elements=[table.c.guild_id, table.c.cycle_id, table.c.channel_id])
        )

    async def record_reply(self, guild_id: int, cycle_id: uuid.UUID, channel_id: int) -> None:
        table = pending_arrest_digest_deliveries
        await self.client.execute(
            sa.update(table)
            .where(
                table.c.guild_id == guild_id,
                table.c.cycle_id == cycle_id,
                table.c.channel_id == channel_id,
            )
            .values(reply_sent=True)
        )

    async def mention_history(
        self, guild_id: int, report_ids: tuple[int, ...]
    ) -> dict[tuple[int, int], tuple[int, datetime]]:
        if not report_ids:
            return {}
        table = pending_arrest_digest_mentions
        rows = await self.client.fetch_all(
            sa.select(table).where(table.c.guild_id == guild_id, table.c.report_id.in_(report_ids))
        )
        return {
            (int(row["report_id"]), int(row["user_id"])): (int(row["stage"]), row["mentioned_at"])
            for row in rows
        }

    async def record_mentions(
        self, guild_id: int, mentions: tuple[tuple[int, int, int], ...], *, now: datetime
    ) -> None:
        table = pending_arrest_digest_mentions
        async with self.client.transaction() as connection:
            for report_id, user_id, stage in mentions:
                await self.client.execute(
                    insert(table)
                    .values(
                        guild_id=guild_id,
                        report_id=report_id,
                        user_id=user_id,
                        stage=stage,
                        mentioned_at=now,
                    )
                    .on_conflict_do_update(
                        index_elements=[table.c.guild_id, table.c.report_id, table.c.user_id],
                        set_={"stage": stage, "mentioned_at": now},
                    ),
                    connection=connection,
                )
