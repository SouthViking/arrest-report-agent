"""PostgreSQL metadata repository for generic agent conversations."""

from __future__ import annotations

import json

import sqlalchemy as sa
from agents.items import TResponseInputItem
from sqlalchemy.dialects.postgresql import insert as pg_insert

from arrest_report_agent.infrastructure.postgres import PostgresClient
from arrest_report_agent.infrastructure.postgres.schema import agent_messages, agent_sessions

from .models import AgentConversationKey, AgentSessionRecord


class AgentSessionRepository:
    def __init__(self, client: PostgresClient) -> None:
        self.client = client

    async def get_or_create(self, key: AgentConversationKey) -> AgentSessionRecord:
        async with self.client.transaction() as connection:
            inserted = await connection.execute(
                pg_insert(agent_sessions)
                .values(
                    session_id=key.session_id,
                    namespace=key.namespace,
                    scope_key=key.scope_key,
                    access_partition=key.access_partition,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        agent_sessions.c.namespace,
                        agent_sessions.c.scope_key,
                        agent_sessions.c.access_partition,
                    ]
                )
                .returning(agent_sessions.c.session_id)
            )
            created_session_id = inserted.scalar_one_or_none()
            row = (
                await connection.execute(
                    sa.select(
                        agent_sessions.c.session_id,
                        agent_sessions.c.source_cursor,
                    ).where(
                        agent_sessions.c.namespace == key.namespace,
                        agent_sessions.c.scope_key == key.scope_key,
                        agent_sessions.c.access_partition == key.access_partition,
                    )
                )
            ).mappings().one()
        return AgentSessionRecord(
            session_id=str(row["session_id"]),
            source_cursor=(str(row["source_cursor"]) if row["source_cursor"] is not None else None),
            created=created_session_id is not None,
        )

    async def advance_source_cursor(self, session_id: str, source_cursor: str) -> None:
        result = await self.client.execute(
            agent_sessions.update()
            .where(agent_sessions.c.session_id == session_id)
            .values(source_cursor=source_cursor, updated_at=sa.func.now())
        )
        if result.rowcount != 1:
            raise LookupError("Agent session disappeared while advancing its source cursor")

    async def read_source_cursor(self, key: AgentConversationKey) -> str | None:
        row = await self.client.fetch_one(
            sa.select(agent_sessions.c.source_cursor).where(
                agent_sessions.c.session_id == key.session_id
            )
        )
        return str(row["source_cursor"]) if row and row["source_cursor"] is not None else None

    async def read_items(
        self,
        key: AgentConversationKey,
        *,
        readable_partitions: tuple[str, ...],
    ) -> list[tuple[str, TResponseInputItem]]:
        """Read partition-tagged items in chronological order for an access-aware view."""

        session_ids = [
            AgentConversationKey(key.namespace, key.scope_key, partition).session_id
            for partition in (key.access_partition, *readable_partitions)
        ]
        rows = await self.client.fetch_all(
            sa.select(agent_messages.c.session_id, agent_messages.c.message_data)
            .where(agent_messages.c.session_id.in_(session_ids))
            .order_by(agent_messages.c.created_at, agent_messages.c.id)
        )
        items: list[tuple[str, TResponseInputItem]] = []
        for row in rows:
            try:
                items.append((str(row["session_id"]), json.loads(row["message_data"])))
            except json.JSONDecodeError:
                # Match the SDK session's handling of a corrupt stored item.
                continue
        return items
