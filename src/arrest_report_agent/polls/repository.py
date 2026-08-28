"""PostgreSQL index for polls created through the agent."""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa

from arrest_report_agent.application.ports import PollListQuery
from arrest_report_agent.infrastructure.postgres import PostgresClient
from arrest_report_agent.infrastructure.postgres.schema import agent_polls

from .models import PollRecord


class PollRepository:
    def __init__(self, client: PostgresClient) -> None:
        self.client = client

    async def create(self, record: PollRecord) -> PollRecord:
        await self.client.execute(agent_polls.insert().values(**record.model_dump()))
        return record

    async def get(self, guild_id: int, message_id: int) -> PollRecord | None:
        row = await self.client.fetch_one(
            sa.select(agent_polls).where(
                agent_polls.c.guild_id == guild_id,
                agent_polls.c.message_id == message_id,
            )
        )
        return None if row is None else PollRecord.model_validate(dict(row))

    async def list(
        self,
        guild_id: int,
        requester_user_id: int,
        query: PollListQuery,
    ) -> tuple[tuple[PollRecord, ...], int]:
        now = datetime.now(UTC)
        filters: list[sa.ColumnElement[bool]] = [agent_polls.c.guild_id == guild_id]
        if query.creator_user_ids:
            filters.append(agent_polls.c.creator_user_id.in_(query.creator_user_ids))
        if query.only_created_by_requester:
            filters.append(agent_polls.c.creator_user_id == requester_user_id)
        if query.channel_ids:
            filters.append(agent_polls.c.channel_id.in_(query.channel_ids))
        if query.created_after is not None:
            filters.append(agent_polls.c.created_at >= query.created_after)
        if query.created_before is not None:
            filters.append(agent_polls.c.created_at <= query.created_before)
        if query.expires_after is not None:
            filters.append(agent_polls.c.expires_at >= query.expires_after)
        if query.expires_before is not None:
            filters.append(agent_polls.c.expires_at <= query.expires_before)
        if query.status == "open":
            filters.extend(
                [
                    agent_polls.c.deleted_at.is_(None),
                    agent_polls.c.ended_at.is_(None),
                    agent_polls.c.expires_at > now,
                ]
            )
        elif query.status == "ended":
            filters.extend(
                [
                    agent_polls.c.deleted_at.is_(None),
                    sa.or_(agent_polls.c.ended_at.is_not(None), agent_polls.c.expires_at <= now),
                ]
            )
        elif query.status == "deleted":
            filters.append(agent_polls.c.deleted_at.is_not(None))

        total_row = await self.client.fetch_one(
            sa.select(sa.func.count().label("count")).select_from(agent_polls).where(*filters)
        )
        total = int(total_row["count"] if total_row is not None else 0)
        ordering = (
            agent_polls.c.created_at.asc()
            if query.sort_direction == "asc"
            else agent_polls.c.created_at.desc()
        )
        rows = await self.client.fetch_all(
            sa.select(agent_polls)
            .where(*filters)
            .order_by(ordering, agent_polls.c.message_id)
            .offset((query.page - 1) * query.limit)
            .limit(query.limit)
        )
        return tuple(PollRecord.model_validate(dict(row)) for row in rows), total

    async def mark_ended(self, guild_id: int, message_id: int, *, at: datetime) -> None:
        await self.client.execute(
            agent_polls.update()
            .where(
                agent_polls.c.guild_id == guild_id,
                agent_polls.c.message_id == message_id,
            )
            .values(ended_at=at, last_synced_at=at)
        )

    async def mark_deleted(self, guild_id: int, message_id: int, *, at: datetime) -> None:
        await self.client.execute(
            agent_polls.update()
            .where(
                agent_polls.c.guild_id == guild_id,
                agent_polls.c.message_id == message_id,
            )
            .values(deleted_at=at, last_synced_at=at)
        )

    async def mark_synced(self, guild_id: int, message_id: int, *, at: datetime) -> None:
        await self.client.execute(
            agent_polls.update()
            .where(
                agent_polls.c.guild_id == guild_id,
                agent_polls.c.message_id == message_id,
            )
            .values(last_synced_at=at)
        )

    async def touch_activity(self, guild_id: int, message_id: int, *, at: datetime) -> None:
        await self.client.execute(
            agent_polls.update()
            .where(
                agent_polls.c.guild_id == guild_id,
                agent_polls.c.message_id == message_id,
            )
            .values(last_activity_at=at)
        )
