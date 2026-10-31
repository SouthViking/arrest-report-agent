"""Regression coverage for withdrawing a role without touching existing memberships or requests."""

from __future__ import annotations

import asyncio
import json
import os
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from arrest_report_agent.infrastructure.postgres import PostgresClient, PostgresClientConfig
from arrest_report_agent.role_requests.models import RoleRequestKind
from arrest_report_agent.role_requests.repository import RoleRequestRepository

LSSD_GUILD_ID = 1234674521416663070
WITHDRAWN_ROLE_ID = 1241871524424712314


def test_withdraw_role_migration_changes_only_available_lssd_configuration(monkeypatch):
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("requires TEST_DATABASE_URL with an isolated PostgreSQL database")
    monkeypatch.setenv("DATABASE_URL", database_url)
    alembic = Config("alembic.ini")
    try:
        command.downgrade(alembic, "base")
        command.upgrade(alembic, "20261022_0093")
        cases, request = asyncio.run(_seed(database_url))
        command.upgrade(alembic, "20261025_0094")
        asyncio.run(_assert_configuration(database_url, cases, request))
        # A data correction must not silently make the withdrawn role available on downgrade.
        command.downgrade(alembic, "20261022_0093")
        asyncio.run(_assert_configuration(database_url, cases, request))
    finally:
        command.downgrade(alembic, "base")


async def _seed(database_url):
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    cases = {}
    try:
        async with client.transaction() as tx:
            for guild_id in (LSSD_GUILD_ID, 999):
                await tx.execute(
                    sa.text("INSERT INTO discord_guilds (discord_id, name) VALUES (:id, 'Role QA')"),
                    {"id": guild_id},
                )
                for number, case in enumerate((
                    "published", "draft", "archived", "custom", "manual", "empty", "only_withdrawn", "clean",
                ), 1):
                    revision_id = uuid4()
                    status = case if case in {"published", "draft", "archived"} else "draft"
                    base_ids = [10, WITHDRAWN_ROLE_ID, 11]
                    if case == "custom":
                        base_ids = [50, str(WITHDRAWN_ROLE_ID), 60]
                    if case == "only_withdrawn":
                        base_ids = [WITHDRAWN_ROLE_ID]
                    if case == "clean":
                        base_ids = [10, 11]
                    payload = {"ftt_entry": {
                        "base_role_ids": base_ids,
                        "stations": [{"key": "ganton", "label": "Ganton", "role_id": 70}],
                        "welcome": {"channel_id": 80, "rules_url": "https://example.org/ftp",
                                    "reference_channel_ids": [81]},
                    }}
                    if case in {"manual", "empty"}:
                        payload = {}
                    catalog = [10, WITHDRAWN_ROLE_ID, 11, 50, 60, 70]
                    if case == "empty":
                        catalog = [WITHDRAWN_ROLE_ID]
                    if case == "clean":
                        catalog.remove(WITHDRAWN_ROLE_ID)
                    expected_payload = json.loads(json.dumps(payload))
                    expected_catalog = list(catalog)
                    changed = guild_id == LSSD_GUILD_ID and status != "archived"
                    if changed:
                        expected_catalog = [role_id for role_id in catalog if role_id != WITHDRAWN_ROLE_ID]
                        if case == "custom":
                            expected_payload["ftt_entry"]["base_role_ids"] = [50, 60]
                        elif case == "only_withdrawn":
                            expected_payload = {}
                        elif case not in {"manual", "empty", "clean"}:
                            expected_payload["ftt_entry"]["base_role_ids"] = [10, 11]
                    await tx.execute(
                        sa.text("INSERT INTO guild_config_revisions (id, guild_id, revision, status) "
                                "VALUES (:id, :guild, :revision, :status)"),
                        {"id": revision_id, "guild": guild_id, "revision": number, "status": status},
                    )
                    await tx.execute(
                        sa.text("INSERT INTO guild_feature_settings "
                                "(id, config_revision_id, feature_key, enabled, schema_version, payload) "
                                "VALUES (:id, :revision, 'role_requests', true, 1, CAST(:payload AS jsonb))"),
                        {"id": uuid4(), "revision": revision_id, "payload": json.dumps(payload)},
                    )
                    for key in ("role_request_allowed_roles", "pager_recipient_roles"):
                        policy_id = uuid4()
                        await tx.execute(
                            sa.text("INSERT INTO guild_access_policies "
                                    "(id, config_revision_id, policy_key, subject_type, mode) "
                                    "VALUES (:id, :revision, :key, 'role', 'restricted')"),
                            {"id": policy_id, "revision": revision_id, "key": key},
                        )
                        for position, role_id in enumerate(catalog):
                            await tx.execute(
                                sa.text("INSERT INTO guild_access_policy_members "
                                        "(id, policy_id, snowflake_id, position) "
                                        "VALUES (:id, :policy, :role, :position)"),
                                {"id": uuid4(), "policy": policy_id, "role": role_id, "position": position},
                            )
                    cases[revision_id] = (expected_payload, expected_catalog, catalog)
        request, _created = await RoleRequestRepository(client).reserve(
            guild_id=LSSD_GUILD_ID, interaction_id=123456, requester_id=42,
            kind=RoleRequestKind.FTT_ENTRY, add_role_ids=(WITHDRAWN_ROLE_ID,), remove_role_ids=(),
            reason="Solicitud anterior", public_channel_id=90,
        )
        return cases, request
    finally:
        await client.close()


async def _assert_configuration(database_url, cases, request):
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    try:
        for revision_id, (expected_payload, expected_catalog, original_catalog) in cases.items():
            feature = await client.fetch_one(
                sa.text("SELECT payload FROM guild_feature_settings WHERE config_revision_id = :id "
                        "AND feature_key = 'role_requests'"),
                {"id": revision_id},
            )
            assert feature["payload"] == expected_payload
            for key, expected in (
                ("role_request_allowed_roles", expected_catalog), ("pager_recipient_roles", original_catalog),
            ):
                members = await client.fetch_all(
                    sa.text("SELECT member.snowflake_id FROM guild_access_policy_members AS member "
                            "JOIN guild_access_policies AS policy ON policy.id = member.policy_id "
                            "WHERE policy.config_revision_id = :id AND policy.policy_key = :key "
                            "ORDER BY member.position"),
                    {"id": revision_id, "key": key},
                )
                assert [int(row["snowflake_id"]) for row in members] == expected
                policy = await client.fetch_one(
                    sa.text("SELECT mode FROM guild_access_policies "
                            "WHERE config_revision_id = :id AND policy_key = :key"),
                    {"id": revision_id, "key": key},
                )
                assert policy["mode"] == ("restricted" if expected else "disabled")
        assert await RoleRequestRepository(client).get(request.id) == request
    finally:
        await client.close()
