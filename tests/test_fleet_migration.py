from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from arrest_report_agent.infrastructure.postgres import PostgresClient, PostgresClientConfig


async def seed_or_check(database_url: str, *, seed: bool) -> None:
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    try:
        if seed:
            async with client.transaction() as tx:
                for guild_id in (1234674521416663070, 999):
                    await tx.execute(
                        sa.text("INSERT INTO discord_guilds (discord_id, name) VALUES (:id, 'fleet-test')"),
                        {"id": guild_id},
                    )
                    await tx.execute(sa.text(
                        "INSERT INTO guild_config_revisions (id, guild_id, revision, status) "
                        "VALUES (:id, :guild, 1, 'published')"
                    ), {"id": uuid4(), "guild": guild_id})
        else:
            rows = await client.fetch_all(sa.text(
                "SELECT r.guild_id, f.enabled, f.payload FROM guild_feature_settings f "
                "JOIN guild_config_revisions r ON r.id = f.config_revision_id WHERE f.feature_key = 'fleet'"
            ))
            assert len(rows) == 1
            assert rows[0]["guild_id"] == 1234674521416663070
            assert rows[0]["enabled"] is True
            assert rows[0]["payload"] == {"faction_id": 3, "cache_seconds": 120}
    finally:
        await client.close()


def test_fleet_migration_enables_only_lssd_and_is_reversible(monkeypatch):
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("requires TEST_DATABASE_URL with an isolated PostgreSQL database")
    monkeypatch.setenv("DATABASE_URL", database_url)
    alembic = Config("alembic.ini")
    command.downgrade(alembic, "base")
    try:
        command.upgrade(alembic, "20260928_0083")
        asyncio.run(seed_or_check(database_url, seed=True))
        command.upgrade(alembic, "head")
        asyncio.run(seed_or_check(database_url, seed=False))
        command.downgrade(alembic, "20260928_0083")
        command.upgrade(alembic, "head")
        asyncio.run(seed_or_check(database_url, seed=False))
    finally:
        command.downgrade(alembic, "base")
