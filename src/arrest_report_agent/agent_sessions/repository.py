"""PostgreSQL metadata repository for generic agent conversations."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from arrest_report_agent.infrastructure.postgres import PostgresClient
from arrest_report_agent.infrastructure.postgres.schema import agent_sessions

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
