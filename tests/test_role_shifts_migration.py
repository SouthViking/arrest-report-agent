from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from arrest_report_agent.config import RoleRequestOptions
from arrest_report_agent.infrastructure.postgres import PostgresClient, PostgresClientConfig
from arrest_report_agent.infrastructure.postgres.schema import (
    discord_guilds,
    guild_access_policies,
    guild_access_policy_members,
    guild_config_revisions,
    guild_feature_settings,
)


def test_shift_migration_scopes_config_and_preserves_existing_choices(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires TEST_DATABASE_URL with isolated PostgreSQL")
    monkeypatch.setenv("DATABASE_URL", url)
    alembic = Config("alembic.ini")
    lssd, other = 1234674521416663070, 999
    custom = [{"key": "custom", "label": "Custom", "role_id": 123, "description": "Horario propio"}]
    targets = [(lssd, 1, None), (lssd, 2, custom), (other, 1, None)]
    revisions = {pair[:2]: uuid4() for pair in targets}

    async def inspect(*, seed=False, migrated=False):
        client = PostgresClient(PostgresClientConfig(url=url))
        await client.open()
        try:
            if seed:
                for guild in (lssd, other):
                    await client.execute(discord_guilds.insert().values(discord_id=guild, name="Shift QA"))
                for guild, revision, shifts in targets:
                    revision_id = revisions[guild, revision]
                    await client.execute(guild_config_revisions.insert().values(
                        id=revision_id, guild_id=guild, revision=revision, status="published",
                    ))
                    await client.execute(guild_feature_settings.insert().values(
                        config_revision_id=revision_id, feature_key="role_requests", enabled=True,
                        payload={"retired_role_id": 456, **({"shifts": shifts} if shifts else {})},
                    ))
                    policy = uuid4()
                    await client.execute(guild_access_policies.insert().values(
                        id=policy, config_revision_id=revision_id, policy_key="role_request_allowed_roles",
                        subject_type="role", mode="restricted",
                    ))
                    await client.execute(guild_access_policy_members.insert().values(
                        policy_id=policy, snowflake_id=456, position=0,
                    ))
                await client.execute(sa.text(
                    "INSERT INTO role_requests (id,guild_id,interaction_id,requester_id,kind,"
                    "add_role_ids,remove_role_ids,status,public_channel_id) "
                    "VALUES (:id,:guild,999,222,'normal',ARRAY[456],ARRAY[]::bigint[],'pending',333)"
                ), {"id": uuid4(), "guild": lssd})
            if migrated:
                for guild, revision, shifts in targets:
                    payload = (await client.fetch_one(sa.select(guild_feature_settings.c.payload).where(
                        guild_feature_settings.c.config_revision_id == revisions[guild, revision],
                        guild_feature_settings.c.feature_key == "role_requests",
                    )))["payload"]
                    assert payload["retired_role_id"] == 456
                    parsed = RoleRequestOptions.model_validate(payload)
                    if guild == other:
                        assert not parsed.shifts
                    elif shifts:
                        assert [option.role_id for option in parsed.shifts] == [123]
                    else:
                        assert [option.role_id for option in parsed.shifts] == [
                            1554676577609187338, 1554676839899996301, 1554676886964142100,
                        ]
                        rows = await client.fetch_all(sa.select(guild_access_policy_members.c.snowflake_id)
                            .join(guild_access_policies)
                            .where(guild_access_policies.c.config_revision_id == revisions[guild, revision]))
                        assert {row["snowflake_id"] for row in rows} == {
                            456, *(option.role_id for option in parsed.shifts),
                        }
                row = await client.fetch_one(sa.text("SELECT * FROM role_requests WHERE interaction_id=999"))
                assert row["status"] == "pending" and row["shift_change"] is None
        finally:
            await client.close()

    command.downgrade(alembic, "base")
    try:
        command.upgrade(alembic, "20260930_0085")
        asyncio.run(inspect(seed=True))
        command.upgrade(alembic, "20260930_0086")
        asyncio.run(inspect(migrated=True))
        command.downgrade(alembic, "20260930_0085")
        # Rollback strips the new config key, preserving old request operations.
        command.upgrade(alembic, "20260930_0086")
    finally:
        command.downgrade(alembic, "base")
