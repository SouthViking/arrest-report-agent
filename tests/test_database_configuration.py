from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from arrest_report_agent.bootstrap.database import (
    DiscordBootstrapResolver,
    DiscordGuildInfo,
    plan_environment_import,
)
from arrest_report_agent.config import (
    AccessPolicyKey,
    AccessPolicyMode,
    AccessPolicySnapshot,
    AppSettings,
    GuildAgentConfig,
    GuildConfigSnapshot,
    GuildFeatureSnapshot,
    GuildRuntimeSettings,
    ModelCatalog,
    PackagedPersonaProvider,
)
from arrest_report_agent.config.database import POLICY_SUBJECT_TYPES
from arrest_report_agent.deployments import DeploymentRepository, RailwayDeploymentEvent
from arrest_report_agent.deployments.repository import DeploymentDeliveryTarget
from arrest_report_agent.infrastructure.postgres import (
    ConfigurationRepository,
    PostgresClient,
    PostgresClientConfig,
)
from arrest_report_agent.infrastructure.postgres.provider import DatabaseConfigProvider
from arrest_report_agent.infrastructure.postgres.repository import BootstrapGuild
from arrest_report_agent.infrastructure.storage import MemoryObjectStorage
from arrest_report_agent.knowledge import ChargeCatalog, ReportGuide


def test_postgres_urls_are_normalized_for_both_drivers() -> None:
    config = PostgresClientConfig(url="postgres://user:secret@host:5432/database")

    assert config.async_url == "postgresql+asyncpg://user:secret@host:5432/database"
    assert config.asyncpg_url == "postgresql://user:secret@host:5432/database"


def test_guild_file_limits_cannot_exceed_process_ceiling() -> None:
    settings = AppSettings(
        _env_file=None,
        DISCORD_TOKEN="discord",
        OPENAI_API_KEY="openai",
        MAX_IMAGES_PER_REVIEW=2,
        MAX_IMAGE_BYTES=1_000,
        MAX_FILES_PER_REVIEW=3,
        MAX_FILE_BYTES=2_000,
        MAX_TOTAL_FILE_BYTES=5_000,
    )
    agent = GuildAgentConfig(
        model=settings.openai_model,
        reasoning_effort=settings.openai_reasoning_effort,
        review_timeout_seconds=settings.review_timeout_seconds,
        max_agent_turns=settings.max_agent_turns,
        max_images_per_review=20,
        max_image_bytes=10_000,
        max_files_per_review=8,
        max_file_bytes=20_000,
        max_total_file_bytes=50_000,
        thread_context_max_messages=settings.thread_context_max_messages,
        channel_context_max_messages=settings.channel_context_max_messages,
        thread_context_max_chars=settings.thread_context_max_chars,
        thread_context_max_message_chars=settings.thread_context_max_message_chars,
        thread_context_max_historical_images=settings.thread_context_max_historical_images,
        thread_context_max_historical_files=settings.thread_context_max_historical_files,
        thread_auto_archive_minutes=settings.thread_auto_archive_minutes,  # type: ignore[arg-type]
    )
    policies = {
        key: AccessPolicySnapshot(
            key=key,
            subject_type=POLICY_SUBJECT_TYPES[key],
            mode=AccessPolicyMode.DISABLED,
        )
        for key in AccessPolicyKey
    }
    snapshot = GuildConfigSnapshot(
        guild_id=1,
        revision_id=uuid4(),
        revision=1,
        agent=agent,
        policies=policies,
        features={},
        persona_revision_id=uuid4(),
        persona_id="walter_vigil",
        persona_revision=1,
    )

    runtime = GuildRuntimeSettings(settings, snapshot)

    assert runtime.max_images_per_review == 2
    assert runtime.max_image_bytes == 1_000
    assert runtime.max_files_per_review == 3
    assert runtime.max_file_bytes == 2_000
    assert runtime.max_total_file_bytes == 5_000


@pytest.mark.asyncio
async def test_legacy_import_partitions_ids_and_disables_empty_restricted_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings(
        _env_file=None,
        DISCORD_TOKEN="discord",
        OPENAI_API_KEY="openai",
        BOT_PERSONA="walter_vigil",
        DISCORD_GUILD_IDS="1,2",
        REPORT_CHANNEL_IDS="10",
        AUTHORIZED_USER_IDS="99",
        SUPERVISOR_ROLE_IDS="20",
        ROLE_REQUEST_ALLOWED_CHANNEL_IDS="11",
        ROLE_REQUEST_ALLOWED_ROLE_IDS="21",
        ROLE_REQUEST_REVIEWER_ROLE_IDS="22",
    )

    async def guilds(_self: object) -> tuple[DiscordGuildInfo, ...]:
        return (DiscordGuildInfo(1, "One"), DiscordGuildInfo(2, "Two"))

    async def channel_guild(_self: object, channel_id: int) -> int:
        return {10: 1, 11: 1}[channel_id]

    async def role_owners(_self: object, _guild_ids: tuple[int, ...]) -> dict[int, int]:
        return {20: 1, 21: 1, 22: 1}

    monkeypatch.setattr(DiscordBootstrapResolver, "guilds", guilds)
    monkeypatch.setattr(DiscordBootstrapResolver, "channel_guild", channel_guild)
    monkeypatch.setattr(DiscordBootstrapResolver, "role_owners", role_owners)

    plan = await plan_environment_import(settings)
    first, second = plan.guilds
    first_policies = {policy.key: policy for policy in first.policies}
    second_policies = {policy.key: policy for policy in second.policies}

    assert first.guild_id == 1
    assert first_policies[AccessPolicyKey.REPORT_CHANNELS].ids == (10,)
    assert first_policies[AccessPolicyKey.AGENT_USERS].ids == (99,)
    assert first_policies[AccessPolicyKey.REPORT_ANALYTICS_ROLES].ids == (20,)
    assert first_policies[AccessPolicyKey.OPERATIONAL_STAFF_ROLES].ids == (20,)
    assert first_policies[AccessPolicyKey.ROLE_REQUEST_REVIEWER_ROLES].ids == (22,)
    assert first.features[0].enabled is True
    assert second.guild_id == 2
    assert second_policies[AccessPolicyKey.REPORT_CHANNELS].mode is AccessPolicyMode.DISABLED
    assert second_policies[AccessPolicyKey.SUPERVISOR_ROLES].mode is AccessPolicyMode.DISABLED
    assert second_policies[AccessPolicyKey.REPORT_ANALYTICS_ROLES].mode is AccessPolicyMode.DISABLED
    assert second_policies[AccessPolicyKey.OPERATIONAL_STAFF_ROLES].mode is AccessPolicyMode.DISABLED
    assert second_policies[AccessPolicyKey.ROLE_REQUEST_REVIEWER_ROLES].mode is AccessPolicyMode.DISABLED
    assert second.features[0].enabled is False


def test_database_migrations_and_versioned_configuration_round_trip() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("requires TEST_DATABASE_URL with an isolated PostgreSQL database")

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        alembic = Config("alembic.ini")
        command.downgrade(alembic, "base")
        command.upgrade(alembic, "head")
        asyncio.run(_exercise_database(database_url))
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def test_callsign_migration_copies_staff_roles_and_publishes_persona() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("requires TEST_DATABASE_URL with an isolated PostgreSQL database")

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    alembic = Config("alembic.ini")
    try:
        command.downgrade(alembic, "base")
        command.upgrade(alembic, "20260812_0005")
        identifiers = asyncio.run(_seed_pre_callsign_configuration(database_url))
        command.upgrade(alembic, "head")
        asyncio.run(_assert_callsign_migration(database_url, identifiers))
    finally:
        command.downgrade(alembic, "base")
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


async def _seed_pre_callsign_configuration(database_url: str) -> dict[str, object]:
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    identifiers: dict[str, object] = {
        "guild_id": 123,
        "persona_id": uuid.uuid4(),
        "persona_revision_id": uuid.uuid4(),
        "config_revision_id": uuid.uuid4(),
    }
    try:
        async with client.transaction() as tx:
            await tx.execute(
                sa.text("INSERT INTO discord_guilds (discord_id, name) VALUES (:guild_id, 'QA')"),
                {"guild_id": identifiers["guild_id"]},
            )
            await tx.execute(
                sa.text("INSERT INTO personas (id, slug) VALUES (:id, 'walter_vigil')"),
                {"id": identifiers["persona_id"]},
            )
            await tx.execute(
                sa.text(
                    "INSERT INTO persona_revisions "
                    "(id, persona_id, revision, schema_version, status, payload, checksum, published_at) "
                    "VALUES (:id, :persona_id, 20, 18, 'published', '{}'::jsonb, :checksum, now())"
                ),
                {
                    "id": identifiers["persona_revision_id"],
                    "persona_id": identifiers["persona_id"],
                    "checksum": "a" * 64,
                },
            )
            await tx.execute(
                sa.text(
                    "INSERT INTO persona_assets "
                    "(id, persona_revision_id, kind, object_key, sha256, content_type, size_bytes) "
                    "VALUES (:id, :revision_id, 'avatar', 'qa/avatar.png', :checksum, 'image/png', 1)"
                ),
                {
                    "id": uuid.uuid4(),
                    "revision_id": identifiers["persona_revision_id"],
                    "checksum": "b" * 64,
                },
            )
            await tx.execute(
                sa.text(
                    "INSERT INTO guild_config_revisions (id, guild_id, revision, status, published_at) "
                    "VALUES (:id, :guild_id, 1, 'published', now())"
                ),
                {
                    "id": identifiers["config_revision_id"],
                    "guild_id": identifiers["guild_id"],
                },
            )
            await tx.execute(
                sa.text(
                    "UPDATE discord_guilds SET active_config_revision_id = :revision_id "
                    "WHERE discord_id = :guild_id"
                ),
                {
                    "revision_id": identifiers["config_revision_id"],
                    "guild_id": identifiers["guild_id"],
                },
            )
            await tx.execute(
                sa.text(
                    "INSERT INTO guild_persona_assignments "
                    "(config_revision_id, persona_revision_id) VALUES (:config_id, :persona_id)"
                ),
                {
                    "config_id": identifiers["config_revision_id"],
                    "persona_id": identifiers["persona_revision_id"],
                },
            )
            await tx.execute(
                sa.text(
                    "INSERT INTO bot_application_profile (singleton_id, persona_revision_id) "
                    "VALUES (1, :persona_id)"
                ),
                {"persona_id": identifiers["persona_revision_id"]},
            )
            for policy_key, role_ids in (
                ("pager_creator_roles", (101, 102, 103)),
                ("supervisor_roles", (104,)),
            ):
                policy_id = uuid.uuid4()
                await tx.execute(
                    sa.text(
                        "INSERT INTO guild_access_policies "
                        "(id, config_revision_id, policy_key, subject_type, mode) "
                        "VALUES (:id, :revision_id, :key, 'role', 'restricted')"
                    ),
                    {
                        "id": policy_id,
                        "revision_id": identifiers["config_revision_id"],
                        "key": policy_key,
                    },
                )
                for position, role_id in enumerate(role_ids):
                    await tx.execute(
                        sa.text(
                            "INSERT INTO guild_access_policy_members "
                            "(id, policy_id, snowflake_id, position) "
                            "VALUES (:id, :policy_id, :role_id, :position)"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "policy_id": policy_id,
                            "role_id": role_id,
                            "position": position,
                        },
                    )
    finally:
        await client.close()
    return identifiers


async def _assert_callsign_migration(
    database_url: str,
    identifiers: dict[str, object],
) -> None:
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    try:
        members = await client.fetch_all(
            sa.text(
                "SELECT member.snowflake_id "
                "FROM guild_access_policies policy "
                "JOIN guild_access_policy_members member ON member.policy_id = policy.id "
                "WHERE policy.config_revision_id = :revision_id "
                "AND policy.policy_key = 'operational_staff_roles' "
                "ORDER BY member.snowflake_id"
            ),
            {"revision_id": identifiers["config_revision_id"]},
        )
        assert [int(row["snowflake_id"]) for row in members] == [101, 102, 103, 104]
        persona = await client.fetch_one(
            sa.text(
                "SELECT pr.revision, pr.schema_version, pr.payload "
                "FROM persona_revisions pr "
                "JOIN guild_persona_assignments assignment ON assignment.persona_revision_id = pr.id "
                "WHERE assignment.config_revision_id = :revision_id"
            ),
            {"revision_id": identifiers["config_revision_id"]},
        )
        assert persona is not None
        assert persona["revision"] == 28
        assert persona["schema_version"] == 26
        assert persona["payload"]["messages"]["interface"]["callsigns"]["saved_title"]
        assert persona["payload"]["messages"]["interface"]["callsigns"]["removed_title"]
        assert persona["payload"]["messages"]["interface"]["callsigns"]["directory_intro"] == (
            "Personal con un callsign registrado en este servidor."
        )
        assert "280L" in persona["payload"]["messages"]["interface"]["callsigns"]["invalid_format"]
        assert persona["payload"]["messages"]["interface"]["deployments"]["release_intro"]
        assert persona["payload"]["messages"]["interface"]["suggestions"]["info_warning"]
        assert persona["payload"]["messages"]["interface"]["commands"]["teamspeak_unauthorized"]
        table = await client.fetch_one(sa.text("SELECT to_regclass('public.guild_member_profiles') AS name"))
        assert table is not None and table["name"] == "guild_member_profiles"
    finally:
        await client.close()


async def _exercise_database(database_url: str) -> None:
    client = PostgresClient(PostgresClientConfig(url=database_url))
    storage = MemoryObjectStorage()
    await client.open()
    try:
        repository = ConfigurationRepository(client, storage)
        loaded = await PackagedPersonaProvider().load("walter_vigil")
        persona = await repository.upsert_persona_revision(
            loaded.profile,
            loaded.avatar_bytes,
            content_type="image/png",
        )
        settings = AppSettings(
            _env_file=None,
            DISCORD_TOKEN="discord",
            OPENAI_API_KEY="openai",
            BOT_PERSONA="walter_vigil",
        )
        policies = tuple(
            AccessPolicySnapshot(
                key=key,
                subject_type=POLICY_SUBJECT_TYPES[key],
                mode=(
                    AccessPolicyMode.OPEN
                    if key
                    in {
                        AccessPolicyKey.REPORT_CHANNELS,
                        AccessPolicyKey.AGENT_USERS,
                        AccessPolicyKey.ROLE_REQUEST_CHANNELS,
                        AccessPolicyKey.ROLE_REQUEST_ALLOWED_ROLES,
                    }
                    else AccessPolicyMode.DISABLED
                ),
            )
            for key in AccessPolicyKey
        )
        from arrest_report_agent.config import GuildAgentConfig

        agent = GuildAgentConfig(
            model=settings.openai_model,
            reasoning_effort=settings.openai_reasoning_effort,
            review_timeout_seconds=settings.review_timeout_seconds,
            max_agent_turns=settings.max_agent_turns,
            max_images_per_review=settings.max_images_per_review,
            max_image_bytes=settings.max_image_bytes,
            max_files_per_review=settings.max_files_per_review,
            max_file_bytes=settings.max_file_bytes,
            max_total_file_bytes=settings.max_total_file_bytes,
            thread_context_max_messages=settings.thread_context_max_messages,
            channel_context_max_messages=settings.channel_context_max_messages,
            thread_context_max_chars=settings.thread_context_max_chars,
            thread_context_max_message_chars=settings.thread_context_max_message_chars,
            thread_context_max_historical_images=settings.thread_context_max_historical_images,
            thread_context_max_historical_files=settings.thread_context_max_historical_files,
            thread_auto_archive_minutes=settings.thread_auto_archive_minutes,  # type: ignore[arg-type]
        )
        async with client.transaction() as tx:
            initial_id = await repository.bootstrap_guild(
                BootstrapGuild(
                    guild_id=123,
                    name="Test Guild",
                    agent=agent,
                    policies=policies,
                    features=(
                        GuildFeatureSnapshot(
                            key="role_requests",
                            enabled=False,
                            schema_version=1,
                        ),
                    ),
                    persona_revision_id=persona.revision_id,
                ),
                connection=tx,
            )
            await repository.set_global_persona(persona.revision_id, connection=tx)

        initial = await repository.active_snapshot(123)
        assert initial.revision_id == initial_id
        assert initial.persona_id == "walter_vigil"
        deployment_repository = DeploymentRepository(client)
        deployment_event = RailwayDeploymentEvent.model_validate(
            {
                "type": "Deployment.deployed",
                "details": {
                    "source": "GitHub",
                    "branch": "main",
                    "commitHash": "a" * 40,
                },
                "resource": {
                    "project": {"id": "project"},
                    "environment": {"id": "production", "isEphemeral": False},
                    "service": {"id": "bot"},
                    "deployment": {"id": "deploy-1"},
                },
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )
        deployment_record, inserted = await deployment_repository.record_event(deployment_event)
        duplicate, inserted_again = await deployment_repository.record_event(deployment_event)
        assert inserted is True
        assert inserted_again is False
        assert duplicate.id == deployment_record.id
        assert (
            await deployment_repository.enqueue_deliveries(
                deployment_record.id,
                (
                    DeploymentDeliveryTarget(
                        guild_id=123,
                        channel_id=456,
                        kind="release",
                        notification_key="release:123:456:0.3.1",
                    ),
                ),
            )
            == 1
        )
        deliveries = await deployment_repository.due_deliveries()
        assert len(deliveries) == 1
        assert deliveries[0].event.commit_hash == "a" * 40
        await deployment_repository.mark_delivery_sent(deliveries[0].id, 789)
        assert await deployment_repository.due_deliveries() == ()
        provider = DatabaseConfigProvider(
            client,
            repository,
            settings,
            ChargeCatalog.load_default(),
            ReportGuide.load_default(),
            ModelCatalog.load_default(),
        )
        assert (await provider.get(123)).snapshot.revision == 1
        draft_id = await repository.create_draft(
            123,
            actor_user_id=7,
            interaction_id=1001,
        )
        draft = await repository.snapshot_for_revision(123, draft_id)
        await repository.update_draft_agent(
            123,
            draft.agent.model_copy(update={"channel_context_max_messages": 9}),
            actor_user_id=7,
            interaction_id=1002,
        )
        published_id = await repository.publish_draft(
            123,
            actor_user_id=7,
            interaction_id=1003,
        )
        published = await repository.active_snapshot(123)
        assert published.revision_id == published_id
        assert published.revision == 2
        assert published.agent.channel_context_max_messages == 9
        assert (await provider.cached(123)).snapshot.revision == 1
        await provider._on_notification(  # noqa: SLF001 - exercises the LISTEN callback boundary.
            json.dumps({"guild_id": 123, "revision_id": str(published_id)})
        )
        assert (await provider.cached(123)).snapshot.revision == 2

        rolled_back_id = await repository.rollback(
            123,
            1,
            actor_user_id=7,
            interaction_id=1004,
        )
        rolled_back = await repository.active_snapshot(123)
        assert rolled_back.revision_id == rolled_back_id
        assert rolled_back.revision == 3
        assert rolled_back.agent.channel_context_max_messages == agent.channel_context_max_messages
        verification = await repository.verify()
        assert verification["ok"] is True
        assert verification["guild_count"] == 1
        assert verification["missing_assets"] == []
    finally:
        await client.close()
