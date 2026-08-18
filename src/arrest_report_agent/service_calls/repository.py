"""Minimal PostgreSQL cursor storage for the service-call feed."""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from arrest_report_agent.infrastructure.postgres.client import PostgresClient
from arrest_report_agent.infrastructure.postgres.schema import service_call_cursors


@dataclass(frozen=True, slots=True)
class ServiceCallCursor:
    guild_id: int
    channel_id: int
    last_call_id: int


class ServiceCallRepository:
    """Store only the newest call delivered to each configured channel."""

    def __init__(self, client: PostgresClient) -> None:
        self.client = client

    async def get(self, guild_id: int, channel_id: int) -> ServiceCallCursor | None:
        row = await self.client.fetch_one(
            sa.select(service_call_cursors).where(
                service_call_cursors.c.guild_id == guild_id,
                service_call_cursors.c.channel_id == channel_id,
            )
        )
        if row is None:
            return None
        return ServiceCallCursor(
            guild_id=int(row["guild_id"]),
            channel_id=int(row["channel_id"]),
            last_call_id=int(row["last_call_id"]),
        )

    async def initialize(
        self,
        guild_id: int,
        channel_id: int,
        last_call_id: int,
    ) -> bool:
        """Create a cursor without replaying calls that predate channel activation."""

        result = await self.client.execute(
            pg_insert(service_call_cursors)
            .values(
                guild_id=guild_id,
                channel_id=channel_id,
                last_call_id=last_call_id,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    service_call_cursors.c.guild_id,
                    service_call_cursors.c.channel_id,
                ]
            )
        )
        return bool(result.rowcount)

    async def advance(
        self,
        guild_id: int,
        channel_id: int,
        call_id: int,
    ) -> None:
        """Advance monotonically after Discord accepted one message."""

        await self.client.execute(
            service_call_cursors.update()
            .where(
                service_call_cursors.c.guild_id == guild_id,
                service_call_cursors.c.channel_id == channel_id,
                service_call_cursors.c.last_call_id < call_id,
            )
            .values(last_call_id=call_id, updated_at=sa.func.now())
        )
