from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from arrest_report_agent.infrastructure.postgres import PostgresClient, PostgresClientConfig
from arrest_report_agent.infrastructure.postgres.schema import (
    discord_guilds,
    guild_config_revisions,
    guild_feature_settings,
)


@pytest.mark.asyncio
async def test_teamspeak_migration_preserves_history_and_other_features(monkeypatch) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("requires TEST_DATABASE_URL with an isolated PostgreSQL database")
    monkeypatch.setenv("DATABASE_URL", database_url)
    alembic = Config("alembic.ini")
    await asyncio.to_thread(command.downgrade, alembic, "base")
    await asyncio.to_thread(command.upgrade, alembic, "20261004_0089")
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    lssd_id = 1234674521416663070
    original = {"address": "51.81.49.72:11189", "other_option": "preserved"}
    try:
        async with client.transaction() as tx:
            for guild_id in (lssd_id, 999):
                await tx.execute(discord_guilds.insert().values(discord_id=guild_id, name="TS3 QA"))
                for number, status in enumerate(("draft", "published", "archived"), start=1):
                    revision_id = uuid4()
                    await tx.execute(guild_config_revisions.insert().values(
                        id=revision_id, guild_id=guild_id, revision=number, status=status,
                    ))
                    for feature_key in ("teamspeak", "unrelated"):
                        await tx.execute(guild_feature_settings.insert().values(
                            config_revision_id=revision_id, feature_key=feature_key,
                            enabled=False, payload=original,
                        ))

        await asyncio.to_thread(command.upgrade, alembic, "head")
        rows = await client.fetch_all(
            sa.select(
                guild_config_revisions.c.guild_id, guild_config_revisions.c.status,
                guild_feature_settings.c.feature_key, guild_feature_settings.c.enabled,
                guild_feature_settings.c.payload,
            ).select_from(guild_config_revisions.join(guild_feature_settings))
        )
        assert len(rows) == 12
        for row in rows:
            should_change = (
                row["guild_id"] == lssd_id and row["status"] != "archived"
                and row["feature_key"] == "teamspeak"
            )
            expected = {**original, "address": "172.96.11.180:11189"} if should_change else original
            assert row["payload"] == expected
            assert row["enabled"] is False
    finally:
        await client.close()
        await asyncio.to_thread(command.downgrade, alembic, "base")
