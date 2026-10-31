"""Durable delivery references, without storing warrant contents."""

from collections.abc import Awaitable, Callable

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from arrest_report_agent.infrastructure.postgres import PostgresClient
from arrest_report_agent.infrastructure.postgres.schema import arrest_warrant_alert_deliveries


class ArrestWarrantAlertRepository:
    def __init__(self, client: PostgresClient) -> None:
        self.client = client

    async def deliver_once(
        self,
        *,
        guild_id: int,
        report_id: int,
        channel_id: int,
        warrant_ids: tuple[int, ...],
        send: Callable[[tuple[int, ...]], Awaitable[int]],
    ) -> bool:
        """Serialize duplicate submissions and persist every delivered warrant reference.

        The caller also uses a deterministic Discord nonce with enforce_nonce for the
        short crash window between Discord delivery and database commit.
        """
        table = arrest_warrant_alert_deliveries
        async with self.client.transaction() as connection:
            await connection.execute(
                sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"arrest-warrant:{guild_id}:{report_id}:{channel_id}"},
            )
            delivered: set[int] = set(
                (
                    await connection.execute(
                        sa.select(table.c.warrant_id).where(
                            table.c.guild_id == guild_id,
                            table.c.sarp_arrest_id == report_id,
                            table.c.channel_id == channel_id,
                        )
                    )
                ).scalars()
            )
            pending = tuple(warrant_id for warrant_id in warrant_ids if warrant_id not in delivered)
            if not pending:
                return False
            message_id = await send(pending)
            await connection.execute(
                insert(table).values(
                    [
                        {
                            "guild_id": guild_id,
                            "sarp_arrest_id": report_id,
                            "channel_id": channel_id,
                            "warrant_id": warrant_id,
                            "message_id": message_id,
                        }
                        for warrant_id in pending
                    ]
                )
            )
        return True
