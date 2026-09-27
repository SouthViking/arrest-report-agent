"""Private attribution for published rumors, keyed by their modal interaction."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from arrest_report_agent.infrastructure.postgres import PostgresClient
from arrest_report_agent.infrastructure.postgres.schema import rumor_publications


class RumorRepository:
    def __init__(self, client: PostgresClient) -> None:
        self.client = client

    async def reserve(
        self,
        *,
        interaction_id: int,
        guild_id: int,
        channel_id: int,
        role_id: int,
        author_user_id: int,
    ) -> bool:
        table = rumor_publications
        result = await self.client.execute(
            insert(table)
            .values(
                interaction_id=interaction_id,
                guild_id=guild_id,
                channel_id=channel_id,
                role_id=role_id,
                author_user_id=author_user_id,
            )
            .on_conflict_do_nothing(index_elements=[table.c.interaction_id])
            .returning(table.c.interaction_id)
        )
        return result.scalar_one_or_none() is not None

    async def mark_published(self, interaction_id: int, message_id: int) -> None:
        table = rumor_publications
        result = await self.client.execute(
            sa.update(table)
            .where(table.c.interaction_id == interaction_id, table.c.message_id.is_(None))
            .values(message_id=message_id)
        )
        if result.rowcount != 1:
            raise RuntimeError("Rumor publication reservation is missing")
