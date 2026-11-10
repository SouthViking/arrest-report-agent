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
async def test_portability_migration_preserves_explicit_choices_and_history(monkeypatch) -> None:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires isolated PostgreSQL")
    monkeypatch.setenv("DATABASE_URL", url)
    alembic = Config("alembic.ini")
    await asyncio.to_thread(command.downgrade, alembic, "base")
    await asyncio.to_thread(command.upgrade, alembic, "20261031_0099")
    client = PostgresClient(PostgresClientConfig(url=url))
    await client.open()
    ids = {case: uuid4() for case in ("current", "custom", "archived", "other")}
    original = {
        "current": {},
        "custom": {
            "support": (False, {"url": "https://example.org/own-support"}),
            "dedeo": (True, {"protected_user_ids": []}),
            "release_routing": (True, {"internal_channel_ids": []}),
            "knowledge_links": (True, {"procedures_manual_url": None}),
        },
        "archived": {},
        "other": {},
    }
    try:
        async with client.transaction() as tx:
            for guild_id in (1234674521416663070, 99):
                await tx.execute(discord_guilds.insert().values(discord_id=guild_id, name="QA"))
            for n, (case, config_id) in enumerate(ids.items(), 1):
                await tx.execute(
                    guild_config_revisions.insert().values(
                        id=config_id,
                        guild_id=99 if case == "other" else 1234674521416663070,
                        revision=n,
                        status="archived"
                        if case == "archived"
                        else ("draft" if case == "custom" else "published"),
                    )
                )
                await tx.execute(
                    guild_feature_settings.insert().values(
                        id=uuid4(),
                        config_revision_id=config_id,
                        feature_key="deployment_notifications",
                        enabled=True,
                        schema_version=1,
                        payload={},
                    )
                )
                for key, (enabled, payload) in original[case].items():
                    await tx.execute(
                        guild_feature_settings.insert().values(
                            id=uuid4(),
                            config_revision_id=config_id,
                            feature_key=key,
                            enabled=enabled,
                            schema_version=1,
                            payload=payload,
                        )
                    )
        await asyncio.to_thread(command.upgrade, alembic, "head")

        async def read():
            rows = await client.fetch_all(
                sa.select(
                    guild_feature_settings.c.config_revision_id,
                    guild_feature_settings.c.feature_key,
                    guild_feature_settings.c.enabled,
                    guild_feature_settings.c.payload,
                )
            )
            return {
                (row["config_revision_id"], row["feature_key"]): (row["enabled"], dict(row["payload"]))
                for row in rows
            }

        first = await read()
        assert first[ids["current"], "support"] == (True, {"url": "https://buymeacoffee.com/southviking"})
        assert first[ids["current"], "dedeo"][1]["protected_user_ids"] == [449044161954447360]
        assert first[ids["current"], "release_routing"][1]["internal_channel_ids"] == [1241206587079524442]
        for key, value in original["custom"].items():
            assert first[ids["custom"], key] == value
        for case in ("archived", "other"):
            assert not any(
                config_id == ids[case] and key != "deployment_notifications" for config_id, key in first
            )
        for case in ids:
            assert first[ids[case], "deployment_notifications"] == (True, {})
        # A rollback and reapplication preserve explicit operator edits and remain idempotent.
        await asyncio.to_thread(command.downgrade, alembic, "20261031_0099")
        assert await read() == first
        await asyncio.to_thread(command.upgrade, alembic, "head")
        assert await read() == first
    finally:
        await client.close()
        await asyncio.to_thread(command.downgrade, alembic, "base")
