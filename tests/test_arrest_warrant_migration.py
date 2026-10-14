from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from arrest_report_agent.config import ArrestReportLookupOptions
from arrest_report_agent.infrastructure.postgres import PostgresClient, PostgresClientConfig
from arrest_report_agent.infrastructure.postgres.schema import (
    discord_guilds,
    guild_config_revisions,
    guild_feature_settings,
)


@pytest.mark.asyncio
async def test_migration_reuses_current_supervisory_destinations_and_preserves_custom_options(monkeypatch):
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("requires isolated PostgreSQL")
    monkeypatch.setenv("DATABASE_URL", database_url)
    alembic = Config("alembic.ini")
    await asyncio.to_thread(command.downgrade, alembic, "base")
    await asyncio.to_thread(command.upgrade, alembic, "20261011_0091")
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    pending_payload = {
        "faction_id": 3,
        "digest_interval_hours": 12,
        "supervisor_alert_channel_ids": [66],
        "supervisor_alert_role_id": 20,
    }
    custom_payload = {
        "faction_id": 3,
        "warrant_alerts_enabled": False,
        "warrant_alert_channel_ids": [77],
        "warrant_alert_role_id": 99,
    }
    revisions = {}
    try:
        async with client.transaction() as tx:
            for guild_id in (1234674521416663070, 999):
                await tx.execute(discord_guilds.insert().values(discord_id=guild_id))
                for number, status in enumerate(("draft", "published", "archived"), start=1):
                    revision_id = uuid4()
                    revisions[(guild_id, status)] = revision_id
                    await tx.execute(
                        guild_config_revisions.insert().values(
                            id=revision_id,
                            guild_id=guild_id,
                            revision=number,
                            status=status,
                        )
                    )
                    await tx.execute(
                        guild_feature_settings.insert().values(
                            config_revision_id=revision_id,
                            feature_key="arrest_report_lookup",
                            enabled=True,
                            payload=(custom_payload if status == "draft" else {"faction_id": 3}),
                        )
                    )
                    await tx.execute(
                        guild_feature_settings.insert().values(
                            config_revision_id=revision_id,
                            feature_key="pending_arrest_reports",
                            enabled=True,
                            payload=pending_payload,
                        )
                    )
        await asyncio.to_thread(command.upgrade, alembic, "head")
        rows = await client.fetch_all(sa.select(guild_feature_settings))
        by_key = {(r["config_revision_id"], r["feature_key"]): r["payload"] for r in rows}
        for (guild_id, status), revision_id in revisions.items():
            assert by_key[(revision_id, "pending_arrest_reports")] == pending_payload
            options = by_key[(revision_id, "arrest_report_lookup")]
            if status == "draft":
                assert options == custom_payload
            elif guild_id == 1234674521416663070 and status == "published":
                assert options == {
                    "faction_id": 3,
                    "warrant_alerts_enabled": True,
                    "warrant_alert_channel_ids": [66],
                    "warrant_alert_role_id": 20,
                }
                ArrestReportLookupOptions.model_validate(options)
            else:
                assert options == {"faction_id": 3}
    finally:
        await client.close()
        await asyncio.to_thread(command.downgrade, alembic, "base")
