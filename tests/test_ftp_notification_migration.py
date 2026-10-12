from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from arrest_report_agent.config import FtpRequestOptions
from arrest_report_agent.config.bootstrap_defaults import ftp_defaults
from arrest_report_agent.infrastructure.postgres import PostgresClient, PostgresClientConfig
from arrest_report_agent.infrastructure.postgres.schema import (
    discord_guilds,
    guild_config_revisions,
    guild_feature_settings,
)

LSSD_GUILD_ID = 1234674521416663070
FTP_MANAGEMENT_ROLE_ID = 1390426066455105598
EXTRA_ROLES_KEY = "supervisor_extra_notification_role_ids"


def test_ftp_default_extra_notification_roles_are_guild_scoped() -> None:
    defaults = ftp_defaults(LSSD_GUILD_ID)
    assert defaults is not None
    assert defaults.supervisor_extra_notification_role_ids == (FTP_MANAGEMENT_ROLE_ID,)
    assert ftp_defaults(999) is None


@pytest.mark.asyncio
async def test_ftp_notification_migration_preserves_existing_choices_and_permissions(monkeypatch) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("requires TEST_DATABASE_URL with an isolated PostgreSQL database")
    monkeypatch.setenv("DATABASE_URL", database_url)
    alembic = Config("alembic.ini")
    await asyncio.to_thread(command.downgrade, alembic, "base")
    await asyncio.to_thread(command.upgrade, alembic, "20261001_0088")
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    base = {"channel_id": 50, "command_role_ids": [60], "executive_role_ids": [70]}
    original = {
        (LSSD_GUILD_ID, 1): base,
        (LSSD_GUILD_ID, 2): {**base, EXTRA_ROLES_KEY: []},
        (LSSD_GUILD_ID, 3): {**base, EXTRA_ROLES_KEY: [99]},
        (999, 1): base,
    }
    try:
        async with client.transaction() as tx:
            for guild_id in (LSSD_GUILD_ID, 999):
                await tx.execute(discord_guilds.insert().values(discord_id=guild_id, name="FTP QA"))
            for (guild_id, revision), payload in original.items():
                revision_id = uuid4()
                await tx.execute(guild_config_revisions.insert().values(
                    id=revision_id, guild_id=guild_id, revision=revision, status="draft",
                ))
                await tx.execute(guild_feature_settings.insert().values(
                    config_revision_id=revision_id, feature_key="ftp_requests", enabled=True, payload=payload,
                ))
                await tx.execute(guild_feature_settings.insert().values(
                    config_revision_id=revision_id, feature_key="unrelated", enabled=False, payload=base,
                ))

        await asyncio.to_thread(command.upgrade, alembic, "head")
        await asyncio.to_thread(command.upgrade, alembic, "head")
        rows = await client.fetch_all(
            sa.select(
                guild_config_revisions.c.guild_id, guild_config_revisions.c.revision,
                guild_feature_settings.c.feature_key, guild_feature_settings.c.payload,
            ).select_from(guild_config_revisions.join(guild_feature_settings)).where(
                guild_feature_settings.c.feature_key.in_(("ftp_requests", "unrelated"))
            )
        )
        for row in rows:
            key = (row["guild_id"], row["revision"])
            payload = row["payload"]
            if row["feature_key"] == "unrelated":
                assert payload == base
                continue
            expected = (
                {**base, EXTRA_ROLES_KEY: [FTP_MANAGEMENT_ROLE_ID]}
                if key == (LSSD_GUILD_ID, 1) else original[key]
            )
            assert payload == expected
            assert FtpRequestOptions.model_validate(payload).management_role_ids == (60, 70)

        await asyncio.to_thread(command.downgrade, alembic, "20261001_0088")
        payloads = await client.fetch_all(sa.select(guild_feature_settings.c.payload))
        assert all(EXTRA_ROLES_KEY not in row["payload"] for row in payloads)
    finally:
        await client.close()
        await asyncio.to_thread(command.downgrade, alembic, "base")
