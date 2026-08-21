"""Minimal PostgreSQL cursor storage for stolen-vehicle notifications."""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from arrest_report_agent.infrastructure.postgres.client import PostgresClient
from arrest_report_agent.infrastructure.postgres.schema import stolen_vehicle_feed_cursors


@dataclass(frozen=True, slots=True)
class StolenVehicleFeedCursor:
    guild_id: int
    channel_id: int
    last_report_id: int


class StolenVehicleFeedRepository:
    """Store only the newest report delivered to each configured channel."""

    def __init__(self, client: PostgresClient) -> None:
        self.client = client

    async def get(
        self,
        guild_id: int,
        channel_id: int,
    ) -> StolenVehicleFeedCursor | None:
        row = await self.client.fetch_one(
            sa.select(stolen_vehicle_feed_cursors).where(
                stolen_vehicle_feed_cursors.c.guild_id == guild_id,
                stolen_vehicle_feed_cursors.c.channel_id == channel_id,
            )
        )
        if row is None:
            return None
        return StolenVehicleFeedCursor(
            guild_id=int(row["guild_id"]),
            channel_id=int(row["channel_id"]),
            last_report_id=int(row["last_report_id"]),
        )

    async def initialize(
        self,
        guild_id: int,
        channel_id: int,
        last_report_id: int,
    ) -> bool:
        result = await self.client.execute(
            pg_insert(stolen_vehicle_feed_cursors)
            .values(
                guild_id=guild_id,
                channel_id=channel_id,
                last_report_id=last_report_id,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    stolen_vehicle_feed_cursors.c.guild_id,
                    stolen_vehicle_feed_cursors.c.channel_id,
                ]
            )
        )
        return bool(result.rowcount)

    async def advance(
        self,
        guild_id: int,
        channel_id: int,
        report_id: int,
    ) -> None:
        await self.client.execute(
            stolen_vehicle_feed_cursors.update()
            .where(
                stolen_vehicle_feed_cursors.c.guild_id == guild_id,
                stolen_vehicle_feed_cursors.c.channel_id == channel_id,
                stolen_vehicle_feed_cursors.c.last_report_id < report_id,
            )
            .values(last_report_id=report_id, updated_at=sa.func.now())
        )
