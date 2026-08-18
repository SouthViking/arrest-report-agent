"""PostgreSQL references linking Discord report threads to SARP arrests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from arrest_report_agent.infrastructure.postgres.schema import report_arrest_bindings

if TYPE_CHECKING:
    from arrest_report_agent.infrastructure.postgres.client import PostgresClient


@dataclass(frozen=True, slots=True)
class ArrestReportBinding:
    guild_id: int
    root_message_id: int
    parent_channel_id: int
    thread_id: int
    sarp_arrest_id: int
    bound_by_user_id: int
    trigger_message_id: int
    created_at: datetime
    updated_at: datetime


class ArrestReportBindingRepository:
    """Store replaceable report references without caching SARP content."""

    def __init__(self, client: PostgresClient) -> None:
        self.client = client

    async def get(self, guild_id: int, root_message_id: int) -> ArrestReportBinding | None:
        row = await self.client.fetch_one(
            sa.select(report_arrest_bindings).where(
                report_arrest_bindings.c.guild_id == guild_id,
                report_arrest_bindings.c.root_message_id == root_message_id,
            )
        )
        return _binding(row) if row is not None else None

    async def bind(
        self,
        *,
        guild_id: int,
        root_message_id: int,
        parent_channel_id: int,
        thread_id: int,
        sarp_arrest_id: int,
        bound_by_user_id: int,
        trigger_message_id: int,
    ) -> ArrestReportBinding:
        statement = (
            pg_insert(report_arrest_bindings)
            .values(
                guild_id=guild_id,
                root_message_id=root_message_id,
                parent_channel_id=parent_channel_id,
                thread_id=thread_id,
                sarp_arrest_id=sarp_arrest_id,
                bound_by_user_id=bound_by_user_id,
                trigger_message_id=trigger_message_id,
            )
            .on_conflict_do_update(
                index_elements=[
                    report_arrest_bindings.c.guild_id,
                    report_arrest_bindings.c.root_message_id,
                ],
                set_={
                    "parent_channel_id": parent_channel_id,
                    "thread_id": thread_id,
                    "sarp_arrest_id": sarp_arrest_id,
                    "bound_by_user_id": bound_by_user_id,
                    "trigger_message_id": trigger_message_id,
                    "updated_at": sa.func.now(),
                },
            )
            .returning(*report_arrest_bindings.c)
        )
        async with self.client.transaction() as connection:
            await connection.execute(
                sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"report-arrest:{guild_id}:{root_message_id}"},
            )
            row = (await connection.execute(statement)).mappings().one()
        return _binding(row)


def _binding(row: sa.RowMapping) -> ArrestReportBinding:
    return ArrestReportBinding(
        guild_id=int(row["guild_id"]),
        root_message_id=int(row["root_message_id"]),
        parent_channel_id=int(row["parent_channel_id"]),
        thread_id=int(row["thread_id"]),
        sarp_arrest_id=int(row["sarp_arrest_id"]),
        bound_by_user_id=int(row["bound_by_user_id"]),
        trigger_message_id=int(row["trigger_message_id"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
