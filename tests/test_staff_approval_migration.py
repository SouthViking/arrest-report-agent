"""Seed only current LSSD configuration, preserving custom and explicitly disabled authority."""

import asyncio
import json
import os
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from arrest_report_agent.infrastructure.postgres import PostgresClient, PostgresClientConfig

GUILD = 1234674521416663070
ROLES = [1234678029662294047, 1234678238182244414]


def test_staff_approval_migration_preserves_scope_and_explicit_options(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires TEST_DATABASE_URL with an isolated PostgreSQL database")
    monkeypatch.setenv("DATABASE_URL", url)
    alembic = Config("alembic.ini")
    try:
        command.downgrade(alembic, "base")
        command.upgrade(alembic, "20261025_0094")
        expected = asyncio.run(_seed(url))
        command.upgrade(alembic, "head")
        asyncio.run(_assert_options(url, expected))
    finally:
        command.downgrade(alembic, "base")


async def _seed(url):
    client = PostgresClient(PostgresClientConfig(url=url))
    await client.open()
    expected = {}
    try:
        async with client.transaction() as tx:
            for guild in (GUILD, 999):
                await tx.execute(
                    sa.text(
                        "INSERT INTO discord_guilds (discord_id, name) VALUES (:id, 'Staff approval QA')"
                    ),
                    {"id": guild},
                )
                for number, kind in enumerate(("published", "draft", "archived", "custom", "disabled"), 1):
                    revision = uuid4()
                    status = kind if kind in {"published", "archived"} else "draft"
                    payload = {"faction_id": 3, "warrant_alerts_enabled": False}
                    if kind == "custom":
                        payload["approval_override_role_ids"] = [50, 60]
                    elif kind == "disabled":
                        payload["approval_override_role_ids"] = []
                    await tx.execute(
                        sa.text(
                            "INSERT INTO guild_config_revisions (id, guild_id, revision, status) "
                            "VALUES (:id, :guild, :revision, :status)"
                        ),
                        {"id": revision, "guild": guild, "revision": number, "status": status},
                    )
                    await tx.execute(
                        sa.text(
                            "INSERT INTO guild_feature_settings "
                            "(id, config_revision_id, feature_key, enabled, schema_version, payload) "
                            "VALUES (:id, :revision, 'arrest_report_lookup', true, 1, "
                            "CAST(:payload AS jsonb))"
                        ),
                        {"id": uuid4(), "revision": revision, "payload": json.dumps(payload)},
                    )
                    if (
                        guild == GUILD
                        and status != "archived"
                        and "approval_override_role_ids" not in payload
                    ):
                        payload["approval_override_role_ids"] = ROLES
                    expected[revision] = payload
    finally:
        await client.close()
    return expected


async def _assert_options(url, expected):
    client = PostgresClient(PostgresClientConfig(url=url))
    await client.open()
    try:
        rows = await client.fetch_all(
            sa.text(
                "SELECT config_revision_id, payload FROM guild_feature_settings "
                "WHERE feature_key = 'arrest_report_lookup'"
            )
        )
        assert {row["config_revision_id"]: row["payload"] for row in rows} == expected
    finally:
        await client.close()
