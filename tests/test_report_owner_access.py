from __future__ import annotations

import asyncio
import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from arrest_report_agent.infrastructure.postgres import PostgresClient, PostgresClientConfig
from arrest_report_agent.infrastructure.postgres.schema import (
    discord_guilds,
    report_owner_agent_runs,
    report_owner_round_grants,
)
from arrest_report_agent.report_owner_access import (
    OwnerRunReservationResult,
    ReportOwnerRunRepository,
)


@pytest.mark.asyncio
async def test_report_owner_reservations_are_persistent_idempotent_and_concurrent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("requires TEST_DATABASE_URL with an isolated PostgreSQL database")
    monkeypatch.setenv("DATABASE_URL", database_url)
    await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    guild_id = 965432101234567890
    root_message_id = 965432101234567891
    owner_user_id = 965432101234567892
    try:
        await client.execute(discord_guilds.delete().where(discord_guilds.c.discord_id == guild_id))
        await client.execute(
            discord_guilds.insert().values(discord_id=guild_id, name="Report owner quota QA")
        )
        repository = ReportOwnerRunRepository(client)

        reservations = await asyncio.gather(
            *(
                repository.reserve(
                    guild_id=guild_id,
                    root_message_id=root_message_id,
                    trigger_message_id=965432101234568000 + index,
                    owner_user_id=owner_user_id,
                    thread_id=root_message_id,
                    max_rounds=5,
                )
                for index in range(7)
            )
        )

        assert sum(
            item.result is OwnerRunReservationResult.RESERVED for item in reservations
        ) == 5
        assert sum(
            item.result is OwnerRunReservationResult.EXHAUSTED for item in reservations
        ) == 2
        assert sorted(
            item.round_number for item in reservations if item.round_number is not None
        ) == [1, 2, 3, 4, 5]

        first = next(
            item for item in reservations if item.result is OwnerRunReservationResult.RESERVED
        )
        duplicate = await repository.reserve(
            guild_id=guild_id,
            root_message_id=root_message_id,
            trigger_message_id=965432101234568000
            + reservations.index(first),
            owner_user_id=owner_user_id,
            thread_id=root_message_id,
            max_rounds=5,
        )
        assert duplicate.result is OwnerRunReservationResult.DUPLICATE
        assert duplicate.used_rounds == 5

        reserved = [item for item in reservations if item.run_id is not None]
        await repository.mark_completed(reserved[0].run_id)  # type: ignore[arg-type]
        await repository.mark_failed(reserved[1].run_id)  # type: ignore[arg-type]
        rows = await client.fetch_all(
            sa.select(report_owner_agent_runs.c.status).where(
                report_owner_agent_runs.c.guild_id == guild_id
            )
        )
        assert len(rows) == 5
        assert {str(row["status"]) for row in rows} == {"started", "completed", "failed"}

        after_restart = ReportOwnerRunRepository(client)
        exhausted = await after_restart.reserve(
            guild_id=guild_id,
            root_message_id=root_message_id,
            trigger_message_id=965432101234568100,
            owner_user_id=owner_user_id,
            thread_id=root_message_id,
            max_rounds=5,
        )
        assert exhausted.result is OwnerRunReservationResult.EXHAUSTED

        grant = await after_restart.grant_extra_rounds(
            guild_id=guild_id,
            root_message_id=root_message_id,
            owner_user_id=owner_user_id,
            granted_by_user_id=965432101234567893,
            interaction_id=965432101234568200,
            amount=2,
            base_max_rounds=5,
        )
        assert grant.added_rounds == 2
        assert grant.used_rounds == 5
        assert grant.max_rounds == 7
        assert grant.remaining_rounds == 2

        repeated = await after_restart.grant_extra_rounds(
            guild_id=guild_id,
            root_message_id=root_message_id,
            owner_user_id=owner_user_id,
            granted_by_user_id=965432101234567893,
            interaction_id=965432101234568200,
            amount=2,
            base_max_rounds=5,
        )
        assert repeated.duplicate is True
        assert repeated.added_rounds == 0
        assert repeated.max_rounds == 7

        after_grant = await asyncio.gather(
            *(
                after_restart.reserve(
                    guild_id=guild_id,
                    root_message_id=root_message_id,
                    trigger_message_id=965432101234568300 + index,
                    owner_user_id=owner_user_id,
                    thread_id=root_message_id,
                    max_rounds=5,
                )
                for index in range(3)
            )
        )
        assert sum(item.result is OwnerRunReservationResult.RESERVED for item in after_grant) == 2
        assert sum(item.result is OwnerRunReservationResult.EXHAUSTED for item in after_grant) == 1
        assert sorted(item.round_number for item in after_grant if item.round_number is not None) == [6, 7]
        grant_rows = await client.fetch_all(
            sa.select(report_owner_round_grants.c.amount).where(
                report_owner_round_grants.c.guild_id == guild_id
            )
        )
        assert [row["amount"] for row in grant_rows] == [2]
    finally:
        await client.execute(discord_guilds.delete().where(discord_guilds.c.discord_id == guild_id))
        await client.close()
