"""Angel Pine is additive, guild-scoped and does not reactivate other stations."""

from __future__ import annotations

import asyncio
import copy
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
from arrest_report_agent.role_requests.models import RoleRequestKind
from arrest_report_agent.role_requests.repository import RoleRequestRepository

GUILD_ID = 1234674521416663070
AP_ROLE_ID = 1358795936033276029
GNT_ROLE_ID = 1258627732594495572
STATION = {"key": "angel_pine", "label": "Angel Pine", "role_id": AP_ROLE_ID, "emoji": "🌲"}


def test_angel_pine_migration_preserves_configuration_and_is_repeatable(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires TEST_DATABASE_URL with an isolated PostgreSQL database")
    monkeypatch.setenv("DATABASE_URL", url)
    alembic = Config("alembic.ini")
    try:
        command.downgrade(alembic, "base")
        command.upgrade(alembic, "20261029_0098")
        cases, request = asyncio.run(_seed(url))
        command.upgrade(alembic, "20261031_0099")
        asyncio.run(_assert_configuration(url, cases, request))
        command.downgrade(alembic, "20261029_0098")
        asyncio.run(_assert_configuration(url, cases, request))
        command.upgrade(alembic, "20261031_0099")
        asyncio.run(_assert_configuration(url, cases, request))
    finally:
        command.downgrade(alembic, "base")


async def _seed(url):
    client = PostgresClient(PostgresClientConfig(url=url))
    await client.open()
    cases = {}
    try:
        async with client.transaction() as tx:
            for guild in (GUILD_ID, 999):
                await tx.execute(discord_guilds.insert().values(discord_id=guild, name="Station QA"))
            for number, kind in enumerate((
                "published", "draft", "archived", "other_guild", "existing_role", "existing_key",
                "manual_only", "disabled", "open", "full", "feature_disabled",
            ), 1):
                revision_id = uuid4()
                guild = 999 if kind == "other_guild" else GUILD_ID
                status = kind if kind in {"published", "draft", "archived"} else "draft"
                mode = kind if kind in {"disabled", "open"} else "restricted"
                stations = [{"key": "ganton", "label": "Ganton", "role_id": GNT_ROLE_ID, "emoji": "🏙️"}]
                catalog = [710, 711, 712, GNT_ROLE_ID]
                if kind == "existing_role":
                    stations.append({**STATION, "key": "custom_ap", "label": "Mi estación"})
                    catalog.append(AP_ROLE_ID)
                elif kind == "existing_key":
                    stations.append({**STATION, "role_id": 80, "label": "Personalizada"})
                    catalog.append(80)
                elif kind == "full":
                    stations.extend({"key": f"station_{i}", "label": f"Estación {i}", "role_id": i}
                                    for i in range(1, 10))
                payload = {
                    "retired_role_id": 99,
                    "shifts": [{"key": "custom", "label": "Turno propio", "role_id": 321,
                                "description": "Horario propio"}],
                    "ftt_entry": {
                        "base_role_ids": [710, 711, 712], "stations": stations,
                        "welcome": {"channel_id": 90, "rules_url": "https://example.org/ftp",
                                    "reference_channel_ids": [91]},
                    },
                }
                if kind == "manual_only":
                    payload.pop("ftt_entry")
                expected_payload = copy.deepcopy(payload)
                expected_catalog = list(catalog)
                changed = guild == GUILD_ID and status != "archived" and mode == "restricted"
                if changed:
                    if AP_ROLE_ID not in expected_catalog:
                        expected_catalog.append(AP_ROLE_ID)
                    if kind not in {"manual_only", "existing_role", "existing_key", "full"}:
                        expected_payload["ftt_entry"]["stations"].append(STATION)
                await tx.execute(guild_config_revisions.insert().values(
                    id=revision_id, guild_id=guild, revision=number, status=status,
                ))
                await tx.execute(guild_feature_settings.insert().values(
                    config_revision_id=revision_id, feature_key="role_requests", schema_version=1,
                    enabled=kind != "feature_disabled", payload=payload,
                ))
                for key in ("role_request_allowed_roles", "pager_recipient_roles"):
                    policy_id = uuid4()
                    await tx.execute(guild_access_policies.insert().values(
                        id=policy_id, config_revision_id=revision_id, policy_key=key,
                        subject_type="role", mode=mode,
                    ))
                    for position, role_id in enumerate(catalog):
                        await tx.execute(guild_access_policy_members.insert().values(
                            policy_id=policy_id, snowflake_id=role_id, position=position,
                        ))
                cases[revision_id] = (expected_payload, expected_catalog, catalog, mode,
                                      kind != "feature_disabled")
        request, _created = await RoleRequestRepository(client).reserve(
            guild_id=GUILD_ID, interaction_id=123456, requester_id=42, kind=RoleRequestKind.FTT_ENTRY,
            add_role_ids=(710, GNT_ROLE_ID), remove_role_ids=(), reason="Ganton",
            public_channel_id=90,
        )
        return cases, request
    finally:
        await client.close()


async def _assert_configuration(url, cases, request):
    client = PostgresClient(PostgresClientConfig(url=url))
    await client.open()
    try:
        for revision_id, (payload, catalog, original_catalog, mode, enabled) in cases.items():
            feature = await client.fetch_one(sa.select(
                guild_feature_settings.c.payload, guild_feature_settings.c.enabled,
            ).where(guild_feature_settings.c.config_revision_id == revision_id))
            assert feature["payload"] == payload
            assert feature["enabled"] is enabled
            RoleRequestOptions.model_validate(feature["payload"])
            for key, expected in (("role_request_allowed_roles", catalog),
                                  ("pager_recipient_roles", original_catalog)):
                rows = await client.fetch_all(sa.select(
                    guild_access_policy_members.c.snowflake_id,
                    guild_access_policy_members.c.position, guild_access_policies.c.mode,
                ).join(guild_access_policies).where(
                    guild_access_policies.c.config_revision_id == revision_id,
                    guild_access_policies.c.policy_key == key,
                ).order_by(guild_access_policy_members.c.position))
                assert [row["snowflake_id"] for row in rows] == expected
                assert [row["position"] for row in rows] == list(range(len(expected)))
                assert all(row["mode"] == mode for row in rows)
        assert await RoleRequestRepository(client).get(request.id) == request
    finally:
        await client.close()
