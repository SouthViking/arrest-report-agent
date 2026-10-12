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
async def test_manual_failure_notification_migration_seeds_current_lssd_and_preserves_custom_options(
    monkeypatch,
):
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("requires TEST_DATABASE_URL with an isolated PostgreSQL database")
    monkeypatch.setenv("DATABASE_URL", database_url)
    alembic = Config("alembic.ini")
    await asyncio.to_thread(command.downgrade, alembic, "base")
    await asyncio.to_thread(command.upgrade, alembic, "20261008_0090")
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    lssd_id = 1234674521416663070
    custom = {"role_ids": [111], "user_ids": [222]}
    try:
        async with client.transaction() as tx:
            for guild_id in (lssd_id, 999):
                await tx.execute(discord_guilds.insert().values(discord_id=guild_id, name="Notification QA"))
                for number, status in enumerate(("draft", "published", "archived"), start=1):
                    revision_id = uuid4()
                    await tx.execute(
                        guild_config_revisions.insert().values(
                            id=revision_id,
                            guild_id=guild_id,
                            revision=number,
                            status=status,
                        )
                    )
                    if guild_id == lssd_id and status == "draft":
                        await tx.execute(
                            guild_feature_settings.insert().values(
                                config_revision_id=revision_id,
                                feature_key="review_failure_notifications",
                                enabled=False,
                                payload=custom,
                            )
                        )
        await asyncio.to_thread(command.upgrade, alembic, "head")
        query = (
            sa.select(
                guild_config_revisions.c.guild_id,
                guild_config_revisions.c.status,
                guild_feature_settings.c.enabled,
                guild_feature_settings.c.payload,
            )
            .select_from(guild_config_revisions.join(guild_feature_settings))
            .where(guild_feature_settings.c.feature_key == "review_failure_notifications")
            .order_by(guild_config_revisions.c.status)
        )
        rows = await client.fetch_all(query)
        assert len(rows) == 2
        assert all(row["guild_id"] == lssd_id for row in rows)
        assert rows[0]["status"] == "draft" and rows[0]["enabled"] is False
        assert rows[0]["payload"] == custom
        assert rows[1]["status"] == "published" and rows[1]["enabled"] is True
        assert rows[1]["payload"] == {
            "role_ids": [1234678029662294047, 1234678238182244414],
            "user_ids": [],
        }
        await asyncio.to_thread(command.downgrade, alembic, "20261008_0090")
        await asyncio.to_thread(command.upgrade, alembic, "head")
        assert await client.fetch_all(query) == rows
    finally:
        await client.close()
        await asyncio.to_thread(command.downgrade, alembic, "base")
