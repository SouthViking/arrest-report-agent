from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from arrest_report_agent.agent_sessions import AgentConversationKey, AgentSessionManager
from arrest_report_agent.bootstrap.database import (
    DiscordBootstrapResolver,
    DiscordGuildInfo,
    plan_environment_import,
)
from arrest_report_agent.ci_notifications import CiNotificationRepository, GitHubWorkflowRunEvent
from arrest_report_agent.ci_notifications.repository import CiDeliveryTarget
from arrest_report_agent.config import (
    AccessPolicyKey,
    AccessPolicyMode,
    AccessPolicySnapshot,
    AppSettings,
    ArrestReportAttachmentMode,
    ArrestReportLookupOptions,
    ArrestStatisticsOptions,
    BotPersona,
    FactionMemberAnalysisOptions,
    FactionReportOptions,
    ForumShortcutsOptions,
    GuildAgentConfig,
    GuildConfigSnapshot,
    GuildFeatureSnapshot,
    GuildRuntimeSettings,
    ModelCatalog,
    PackagedPersonaProvider,
    PendingArrestReportsOptions,
    RadioAuditOptions,
    RoleRequestFttEntryOptions,
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


def test_agent_user_blacklist_requires_explicit_ids_or_disabled_mode() -> None:
    with pytest.raises(ValueError, match="explicit IDs or disabled"):
        AccessPolicySnapshot(
            key=AccessPolicyKey.AGENT_BLOCKED_USERS,
            subject_type="user",
            mode=AccessPolicyMode.OPEN,
        )


def test_arrest_lookup_configuration_requires_positive_faction_id() -> None:
    defaults = ArrestReportLookupOptions(faction_id=3)
    assert defaults.faction_id == 3
    assert defaults.report_attachment_mode is ArrestReportAttachmentMode.GENERATED
    manual = ArrestReportLookupOptions(
        faction_id=3,
        report_attachment_mode="manual",
    )
    assert manual.report_attachment_mode is ArrestReportAttachmentMode.MANUAL
    with pytest.raises(ValueError, match="greater than 0"):
        ArrestReportLookupOptions(faction_id=0)


def test_pending_arrest_reports_options_are_bounded() -> None:
    options = PendingArrestReportsOptions(faction_id=3)
    assert options.max_range_days == 90
    assert options.max_records == 1000
    with pytest.raises(ValueError, match="less than or equal to 365"):
        PendingArrestReportsOptions(faction_id=3, max_range_days=366)
    with pytest.raises(ValueError, match="greater than or equal to 50"):
        PendingArrestReportsOptions(faction_id=3, max_records=49)


def test_arrest_statistics_options_are_bounded() -> None:
    options = ArrestStatisticsOptions(faction_id=3)
    assert options.max_range_days == 30
    assert options.max_reports == 200
    assert options.cache_ttl_seconds == 900
    assert options.detail_concurrency == 3
    assert options.max_top == 10
    with pytest.raises(ValueError, match="less than or equal to 90"):
        ArrestStatisticsOptions(faction_id=3, max_range_days=91)
    with pytest.raises(ValueError, match="less than or equal to 1000"):
        ArrestStatisticsOptions(faction_id=3, max_reports=1_001)
    with pytest.raises(ValueError, match="less than or equal to 5"):
        ArrestStatisticsOptions(faction_id=3, detail_concurrency=6)


def test_faction_report_options_are_bounded() -> None:
    options = FactionReportOptions(faction_id=3)
    assert options.max_range_days == 366
    assert options.max_bank_records == 10_000
    with pytest.raises(ValueError, match="less than or equal to 730"):
        FactionReportOptions(faction_id=3, max_range_days=731)
    with pytest.raises(ValueError, match="greater than or equal to 500"):
        FactionReportOptions(faction_id=3, max_bank_records=499)


def test_faction_member_analysis_options_are_typed_and_bounded() -> None:
    options = FactionMemberAnalysisOptions(
        faction_id=3,
        base_divisions=("GNT", "DLM"),
    )
    assert options.inactivity_warning_days == 14
    assert options.inactivity_critical_days == 30
    assert options.inactivity_warning_percentage == 20
    assert options.inactivity_critical_percentage == 30
    assert options.off_duty_alert_min_connected == 5
    assert options.off_duty_alert_percentage == 70
    with pytest.raises(ValueError, match="at least 1 item"):
        FactionMemberAnalysisOptions(faction_id=3, base_divisions=())
    with pytest.raises(ValueError, match="critical inactivity days"):
        FactionMemberAnalysisOptions(
            faction_id=3,
            base_divisions=("GNT", "DLM"),
            inactivity_warning_days=30,
            inactivity_critical_days=30,
        )
    with pytest.raises(ValueError, match="critical inactivity percentage"):
        FactionMemberAnalysisOptions(
            faction_id=3,
            base_divisions=("GNT", "DLM"),
            inactivity_warning_percentage=40,
            inactivity_critical_percentage=30,
        )
    with pytest.raises(ValueError, match="non-empty and unique"):
        FactionMemberAnalysisOptions(
            faction_id=3,
            base_divisions=("GNT", "gnt"),
        )
    with pytest.raises(ValueError, match="greater than or equal to 2"):
        FactionMemberAnalysisOptions(
            faction_id=3,
            base_divisions=("GNT", "DLM"),
            off_duty_alert_min_connected=1,
        )
    with pytest.raises(ValueError, match="less than or equal to 100"):
        FactionMemberAnalysisOptions(
            faction_id=3,
            base_divisions=("GNT", "DLM"),
            off_duty_alert_percentage=101,
        )


def test_radio_audit_options_are_typed_and_keep_ai_within_query_limits() -> None:
    options = RadioAuditOptions(faction_id=3)
    assert options.query_max_range_days == 30
    assert options.query_max_records == 2_000
    assert options.audit_max_range_days == 7
    assert options.audit_max_records == 500
    with pytest.raises(ValueError, match="audit range"):
        RadioAuditOptions(
            faction_id=3,
            query_max_range_days=7,
            audit_max_range_days=8,
        )
    with pytest.raises(ValueError, match="audit records"):
        RadioAuditOptions(
            faction_id=3,
            query_max_records=100,
            audit_max_records=101,
        )


def test_forum_shortcut_options_are_typed_and_bounded() -> None:
    options = ForumShortcutsOptions.model_validate(
        {
            "organization_name": "LSSD",
            "description": "Accesos oficiales.",
            "banner_url": "https://example.com/banner.gif",
            "sections": [
                {
                    "title": "Foro",
                    "links": [{"label": "Inicio", "url": "https://example.com/forum"}],
                }
            ],
        }
    )
    assert options.organization_name == "LSSD"
    assert options.sections[0].links[0].label == "Inicio"


def test_role_request_retirement_requires_explicit_managed_catalog(settings) -> None:
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
    policies = {
        key: AccessPolicySnapshot(
            key=key,
            subject_type=POLICY_SUBJECT_TYPES[key],
            mode=AccessPolicyMode.DISABLED,
        )
        for key in AccessPolicyKey
    }
    retired_role_id = 1255224113757294702
    policies[AccessPolicyKey.ROLE_REQUEST_ALLOWED_ROLES] = AccessPolicySnapshot(
        key=AccessPolicyKey.ROLE_REQUEST_ALLOWED_ROLES,
        subject_type="role",
        mode=AccessPolicyMode.RESTRICTED,
        ids=(retired_role_id,),
    )
    policies[AccessPolicyKey.ROLE_REQUEST_REVIEW_CHANNELS] = AccessPolicySnapshot(
        key=AccessPolicyKey.ROLE_REQUEST_REVIEW_CHANNELS,
        subject_type="channel",
        mode=AccessPolicyMode.RESTRICTED,
        ids=(1261076248541532240, 1234678185124298853),
    )
    values = {
        "guild_id": 1,
        "revision_id": uuid4(),
        "revision": 1,
        "agent": agent,
        "policies": policies,
        "features": {
            "role_requests": GuildFeatureSnapshot(
                key="role_requests",
                enabled=True,
                schema_version=1,
                payload={"retired_role_id": retired_role_id},
            )
        },
        "persona_revision_id": uuid4(),
        "persona_id": "walter_vigil",
        "persona_revision": 1,
    }

    snapshot = GuildConfigSnapshot.model_validate(values)
    runtime = GuildRuntimeSettings(settings, snapshot)
    assert runtime.role_request_retired_role_id == retired_role_id
    assert runtime.role_request_review_channel_ids == (
        1261076248541532240,
        1234678185124298853,
    )

    invalid = snapshot.model_dump()
    invalid["policies"][AccessPolicyKey.ROLE_REQUEST_ALLOWED_ROLES]["mode"] = AccessPolicyMode.OPEN
    invalid["policies"][AccessPolicyKey.ROLE_REQUEST_ALLOWED_ROLES]["ids"] = ()
    with pytest.raises(ValueError, match="explicit managed-role catalog"):
        GuildConfigSnapshot.model_validate(invalid)

    invalid = snapshot.model_dump()
    invalid["features"]["role_requests"]["payload"]["retired_role_id"] = 999
    with pytest.raises(ValueError, match="must belong to the managed-role catalog"):
        GuildConfigSnapshot.model_validate(invalid)


def test_role_request_ftt_entry_requires_managed_unique_roles() -> None:
    options = RoleRequestFttEntryOptions.model_validate(
        {
            "base_role_ids": [10, 11, 12, 13],
            "stations": [
                {"key": "ganton", "label": "Ganton", "role_id": 14, "emoji": "🏙️"},
                {
                    "key": "dillimore",
                    "label": "Dillimore",
                    "role_id": 15,
                    "emoji": "🌲",
                },
            ],
            "welcome": {
                "channel_id": 20,
                "rules_url": "https://sheriff.example/ftp",
                "reference_channel_ids": [21, 22],
            },
        }
    )

    assert options.base_role_ids == (10, 11, 12, 13)
    assert [station.key for station in options.stations] == ["ganton", "dillimore"]
    assert options.welcome is not None
    assert options.welcome.channel_id == 20
    assert options.welcome.reference_channel_ids == (21, 22)

    with pytest.raises(ValueError, match="base and station roles must be distinct"):
        RoleRequestFttEntryOptions.model_validate(
            {
                "base_role_ids": [10, 11],
                "stations": [{"key": "ganton", "label": "Ganton", "role_id": 10}],
            }
        )


def test_arrest_lookup_runtime_requires_valid_enabled_payload(settings) -> None:
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
    policies = {
        key: AccessPolicySnapshot(
            key=key,
            subject_type=POLICY_SUBJECT_TYPES[key],
            mode=AccessPolicyMode.DISABLED,
        )
        for key in AccessPolicyKey
    }
    values = {
        "guild_id": 1,
        "revision_id": uuid4(),
        "revision": 1,
        "agent": agent,
        "policies": policies,
        "features": {
            "arrest_report_lookup": GuildFeatureSnapshot(
                key="arrest_report_lookup",
                enabled=True,
                schema_version=1,
                payload={"faction_id": 3},
            )
        },
        "persona_revision_id": uuid4(),
        "persona_id": "walter_vigil",
        "persona_revision": 1,
    }

    snapshot = GuildConfigSnapshot.model_validate(values)
    assert GuildRuntimeSettings(settings, snapshot).arrest_report_lookup_faction_id == 3
    assert (
        GuildRuntimeSettings(settings, snapshot).arrest_report_attachment_mode
        is ArrestReportAttachmentMode.GENERATED
    )
    values["features"]["arrest_report_lookup"] = GuildFeatureSnapshot(
        key="arrest_report_lookup",
        enabled=True,
        schema_version=1,
        payload={"faction_id": 3, "report_attachment_mode": "generated"},
    )
    generated_snapshot = GuildConfigSnapshot.model_validate(values)
    assert (
        GuildRuntimeSettings(settings, generated_snapshot).arrest_report_attachment_mode
        is ArrestReportAttachmentMode.GENERATED
    )
    values["features"] = {
        "arrest_report_lookup": GuildFeatureSnapshot(
            key="arrest_report_lookup",
            enabled=True,
            schema_version=1,
            payload={},
        )
    }
    with pytest.raises(ValueError, match="faction_id"):
        GuildConfigSnapshot.model_validate(values)


def test_report_owner_access_requires_explicit_channels_and_bounded_rounds(settings) -> None:
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
    policies = {
        key: AccessPolicySnapshot(
            key=key,
            subject_type=POLICY_SUBJECT_TYPES[key],
            mode=AccessPolicyMode.DISABLED,
        )
        for key in AccessPolicyKey
    }
    policies[AccessPolicyKey.REPORT_CHANNELS] = AccessPolicySnapshot(
        key=AccessPolicyKey.REPORT_CHANNELS,
        subject_type="channel",
        mode=AccessPolicyMode.RESTRICTED,
        ids=(123,),
    )
    snapshot = GuildConfigSnapshot(
        guild_id=1,
        revision_id=uuid4(),
        revision=1,
        agent=agent,
        policies=policies,
        features={
            "report_owner_access": GuildFeatureSnapshot(
                key="report_owner_access",
                enabled=True,
                schema_version=1,
                payload={"max_rounds": 5},
            )
        },
        persona_revision_id=uuid4(),
        persona_id="walter_vigil",
        persona_revision=1,
    )

    assert GuildRuntimeSettings(settings, snapshot).report_owner_access_max_rounds == 5
    invalid = snapshot.model_dump()
    invalid["features"]["report_owner_access"]["payload"] = {"max_rounds": 21}
    with pytest.raises(ValueError, match="less than or equal to 20"):
        GuildConfigSnapshot.model_validate(invalid)
    invalid = snapshot.model_dump()
    invalid["policies"][AccessPolicyKey.REPORT_CHANNELS]["mode"] = AccessPolicyMode.OPEN
    invalid["policies"][AccessPolicyKey.REPORT_CHANNELS]["ids"] = ()
    with pytest.raises(ValueError, match="explicit restricted report channels"):
        GuildConfigSnapshot.model_validate(invalid)


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
    assert first_policies[AccessPolicyKey.AGENT_ROLES].mode is AccessPolicyMode.DISABLED
    assert first_policies[AccessPolicyKey.AGENT_BLOCKED_USERS].mode is AccessPolicyMode.DISABLED
    assert first_policies[AccessPolicyKey.REPORT_ANALYTICS_ROLES].ids == (20,)
    assert first_policies[AccessPolicyKey.OPERATIONAL_STAFF_ROLES].ids == (20,)
    assert first_policies[AccessPolicyKey.FACTION_REPORT_ROLES].mode is AccessPolicyMode.DISABLED
    assert first_policies[AccessPolicyKey.ROLE_REQUEST_REVIEWER_ROLES].ids == (22,)
    assert first.features[0].enabled is True
    assert second.guild_id == 2
    assert second_policies[AccessPolicyKey.REPORT_CHANNELS].mode is AccessPolicyMode.DISABLED
    assert second_policies[AccessPolicyKey.SUPERVISOR_ROLES].mode is AccessPolicyMode.DISABLED
    assert second_policies[AccessPolicyKey.REPORT_ANALYTICS_ROLES].mode is AccessPolicyMode.DISABLED
    assert second_policies[AccessPolicyKey.OPERATIONAL_STAFF_ROLES].mode is AccessPolicyMode.DISABLED
    assert second_policies[AccessPolicyKey.FACTION_REPORT_ROLES].mode is AccessPolicyMode.DISABLED
    assert second_policies[AccessPolicyKey.ROLE_REQUEST_REVIEWER_ROLES].mode is AccessPolicyMode.DISABLED
    assert second.features[0].enabled is False


@pytest.mark.asyncio
async def test_lssd_legacy_import_uses_command_and_executive_role_request_review_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild_id = 1234674521416663070
    previous_reviewer_role_id = 1527777009219010760
    settings = AppSettings(
        _env_file=None,
        DISCORD_TOKEN="discord",
        OPENAI_API_KEY="openai",
        BOT_PERSONA="walter_vigil",
        DISCORD_GUILD_IDS=str(guild_id),
        ROLE_REQUEST_REVIEWER_ROLE_IDS=str(previous_reviewer_role_id),
    )

    async def guilds(_self: object) -> tuple[DiscordGuildInfo, ...]:
        return (DiscordGuildInfo(guild_id, "LSSD"),)

    async def channel_guild(_self: object, _channel_id: int) -> int:
        raise AssertionError("No legacy channel lookup was expected")

    async def role_owners(_self: object, _guild_ids: tuple[int, ...]) -> dict[int, int]:
        return {previous_reviewer_role_id: guild_id}

    monkeypatch.setattr(DiscordBootstrapResolver, "guilds", guilds)
    monkeypatch.setattr(DiscordBootstrapResolver, "channel_guild", channel_guild)
    monkeypatch.setattr(DiscordBootstrapResolver, "role_owners", role_owners)

    plan = await plan_environment_import(settings)
    policies = {policy.key: policy for policy in plan.guilds[0].policies}

    assert policies[AccessPolicyKey.ROLE_REQUEST_REVIEW_CHANNELS].ids == (1261076248541532240,)
    assert policies[AccessPolicyKey.ROLE_REQUEST_REVIEWER_ROLES].ids == (
        1234678029662294047,
        1234678238182244414,
    )
    assert plan.guilds[0].features[0].enabled is True


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


def test_scope_refusal_cleanup_migration_sanitizes_persisted_personas() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("requires TEST_DATABASE_URL with an isolated PostgreSQL database")

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    alembic = Config("alembic.ini")
    try:
        command.downgrade(alembic, "base")
        command.upgrade(alembic, "20260901_0060")
        seeded_count = asyncio.run(_seed_legacy_scope_refusals(database_url))

        command.upgrade(alembic, "head")

        asyncio.run(_assert_scope_refusals_removed(database_url, seeded_count))
    finally:
        command.downgrade(alembic, "base")
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


def test_global_persona_repair_migration_updates_an_already_applied_revision() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("requires TEST_DATABASE_URL with an isolated PostgreSQL database")

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    alembic = Config("alembic.ini")
    try:
        command.downgrade(alembic, "base")
        command.upgrade(alembic, "20260812_0005")
        asyncio.run(_seed_pre_callsign_configuration(database_url))
        command.upgrade(alembic, "20260824_0047")
        asyncio.run(_set_global_persona_revision(database_url, "walter_vigil", 51))
        asyncio.run(_assert_global_persona_revision(database_url, "walter_vigil", 51, 44))

        command.upgrade(alembic, "head")

        asyncio.run(_assert_global_persona_revision(database_url, "walter_vigil", 58, 47))
    finally:
        command.downgrade(alembic, "base")
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def test_callsign_alphabet_migration_adds_chat_without_replacing_deployment_channel() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("requires TEST_DATABASE_URL with an isolated PostgreSQL database")

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    alembic = Config("alembic.ini")
    try:
        command.downgrade(alembic, "base")
        command.upgrade(alembic, "20260813_0013")
        asyncio.run(_seed_deployment_channel_configuration(database_url))
        command.upgrade(alembic, "head")
        asyncio.run(
            _assert_deployment_channels(
                database_url,
                (1234675609536430152, 1241206587079524442),
            )
        )
        asyncio.run(_assert_ci_failure_channels(database_url, (1241206587079524442,)))
        asyncio.run(
            _assert_deployment_incident_channels(
                database_url,
                (1241206587079524442,),
            )
        )
        command.downgrade(alembic, "20260813_0013")
        asyncio.run(_assert_deployment_channels(database_url, (1241206587079524442,)))
        asyncio.run(_assert_ci_failure_channels(database_url, ()))
        asyncio.run(_assert_deployment_incident_channels(database_url, ()))
    finally:
        command.downgrade(alembic, "base")
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def test_service_call_migration_configures_lssd_without_enabling_other_guilds() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("requires TEST_DATABASE_URL with an isolated PostgreSQL database")

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    alembic = Config("alembic.ini")
    try:
        command.downgrade(alembic, "base")
        command.upgrade(alembic, "20260817_0030")
        asyncio.run(_seed_service_call_configuration_targets(database_url))
        command.upgrade(alembic, "head")
        asyncio.run(_assert_service_call_configuration(database_url))
    finally:
        command.downgrade(alembic, "base")
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def test_logbook_channel_correction_targets_the_actual_lssd_logbook() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("requires TEST_DATABASE_URL with an isolated PostgreSQL database")

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    alembic = Config("alembic.ini")
    try:
        command.downgrade(alembic, "base")
        command.upgrade(alembic, "20260819_0038")
        asyncio.run(_seed_service_call_configuration_targets(database_url))
        command.upgrade(alembic, "20260819_0039")
        asyncio.run(_assert_logbook_channel(database_url, 1261076248541532240))
        command.upgrade(alembic, "head")
        asyncio.run(_assert_logbook_channel(database_url, 1238385605264539709))
        command.downgrade(alembic, "20260819_0039")
        asyncio.run(_assert_logbook_channel(database_url, 1261076248541532240))
    finally:
        command.downgrade(alembic, "base")
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def test_arrest_lookup_migration_enables_only_lssd_and_creates_bindings() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("requires TEST_DATABASE_URL with an isolated PostgreSQL database")

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    alembic = Config("alembic.ini")
    try:
        command.downgrade(alembic, "base")
        command.upgrade(alembic, "20260817_0032")
        asyncio.run(_seed_service_call_configuration_targets(database_url))
        asyncio.run(_seed_pager_forward_configuration(database_url))
        asyncio.run(_seed_role_request_configuration_target(database_url))
        command.upgrade(alembic, "head")
        asyncio.run(_assert_arrest_lookup_configuration(database_url))
        asyncio.run(
            _assert_pager_forward_configuration(
                database_url,
                lssd_channel_id=1234675609536430152,
            )
        )
        command.downgrade(alembic, "20260830_0054")
        asyncio.run(_assert_pager_forward_configuration(database_url, lssd_channel_id=None))
    finally:
        command.downgrade(alembic, "base")
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def test_cobra_pager_recipient_migration_appends_only_to_lssd() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("requires TEST_DATABASE_URL with an isolated PostgreSQL database")

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    alembic = Config("alembic.ini")
    try:
        command.downgrade(alembic, "base")
        command.upgrade(alembic, "20260913_0065")
        asyncio.run(_seed_pager_recipient_configuration(database_url))

        command.upgrade(alembic, "head")

        asyncio.run(_assert_cobra_pager_recipient_configuration(database_url))
    finally:
        command.downgrade(alembic, "base")
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def test_executive_role_request_reviewer_migration_appends_only_to_lssd() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("requires TEST_DATABASE_URL with an isolated PostgreSQL database")

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    alembic = Config("alembic.ini")
    try:
        command.downgrade(alembic, "base")
        command.upgrade(alembic, "20260916_0066")
        asyncio.run(_seed_role_request_reviewer_configuration(database_url))

        command.upgrade(alembic, "head")

        asyncio.run(_assert_executive_role_request_reviewer_configuration(database_url))
    finally:
        command.downgrade(alembic, "base")
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


async def _seed_role_request_reviewer_configuration(database_url: str) -> None:
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    try:
        async with client.transaction() as tx:
            for guild_id, role_ids in (
                (1234674521416663070, (1234678029662294047,)),
                (999, (444,)),
            ):
                revision_id = uuid.uuid4()
                policy_id = uuid.uuid4()
                await tx.execute(
                    sa.text(
                        "INSERT INTO discord_guilds (discord_id, name) VALUES (:guild_id, 'Role request QA')"
                    ),
                    {"guild_id": guild_id},
                )
                await tx.execute(
                    sa.text(
                        "INSERT INTO guild_config_revisions "
                        "(id, guild_id, revision, status, published_at) "
                        "VALUES (:id, :guild_id, 1, 'published', now())"
                    ),
                    {"id": revision_id, "guild_id": guild_id},
                )
                await tx.execute(
                    sa.text(
                        "UPDATE discord_guilds SET active_config_revision_id = :revision_id "
                        "WHERE discord_id = :guild_id"
                    ),
                    {"revision_id": revision_id, "guild_id": guild_id},
                )
                await tx.execute(
                    sa.text(
                        "INSERT INTO guild_access_policies "
                        "(id, config_revision_id, policy_key, subject_type, mode) "
                        "VALUES (:id, :revision_id, 'role_request_reviewer_roles', "
                        "'role', 'restricted')"
                    ),
                    {"id": policy_id, "revision_id": revision_id},
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


async def _assert_executive_role_request_reviewer_configuration(database_url: str) -> None:
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    try:
        rows = await client.fetch_all(
            sa.text(
                "SELECT guild.discord_id, member.snowflake_id, member.position "
                "FROM discord_guilds AS guild "
                "JOIN guild_access_policies AS policy "
                "ON policy.config_revision_id = guild.active_config_revision_id "
                "AND policy.policy_key = 'role_request_reviewer_roles' "
                "JOIN guild_access_policy_members AS member ON member.policy_id = policy.id "
                "ORDER BY guild.discord_id, member.position"
            )
        )
        by_guild: dict[int, list[int]] = {}
        for row in rows:
            by_guild.setdefault(int(row["discord_id"]), []).append(int(row["snowflake_id"]))
        assert by_guild[1234674521416663070] == [
            1234678029662294047,
            1234678238182244414,
        ]
        assert by_guild[999] == [444]
    finally:
        await client.close()


async def _seed_pager_recipient_configuration(database_url: str) -> None:
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    try:
        async with client.transaction() as tx:
            for guild_id, role_ids in (
                (1234674521416663070, (111, 222)),
                (999, (333,)),
            ):
                revision_id = uuid.uuid4()
                policy_id = uuid.uuid4()
                await tx.execute(
                    sa.text("INSERT INTO discord_guilds (discord_id, name) VALUES (:guild_id, 'Pager QA')"),
                    {"guild_id": guild_id},
                )
                await tx.execute(
                    sa.text(
                        "INSERT INTO guild_config_revisions "
                        "(id, guild_id, revision, status, published_at) "
                        "VALUES (:id, :guild_id, 1, 'published', now())"
                    ),
                    {"id": revision_id, "guild_id": guild_id},
                )
                await tx.execute(
                    sa.text(
                        "UPDATE discord_guilds SET active_config_revision_id = :revision_id "
                        "WHERE discord_id = :guild_id"
                    ),
                    {"revision_id": revision_id, "guild_id": guild_id},
                )
                await tx.execute(
                    sa.text(
                        "INSERT INTO guild_access_policies "
                        "(id, config_revision_id, policy_key, subject_type, mode) "
                        "VALUES (:id, :revision_id, 'pager_recipient_roles', "
                        "'role', 'restricted')"
                    ),
                    {"id": policy_id, "revision_id": revision_id},
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


async def _assert_cobra_pager_recipient_configuration(database_url: str) -> None:
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    try:
        rows = await client.fetch_all(
            sa.text(
                "SELECT guild.discord_id, member.snowflake_id, member.position "
                "FROM discord_guilds AS guild "
                "JOIN guild_access_policies AS policy "
                "ON policy.config_revision_id = guild.active_config_revision_id "
                "AND policy.policy_key = 'pager_recipient_roles' "
                "JOIN guild_access_policy_members AS member ON member.policy_id = policy.id "
                "ORDER BY guild.discord_id, member.position"
            )
        )
        by_guild: dict[int, list[int]] = {}
        for row in rows:
            by_guild.setdefault(int(row["discord_id"]), []).append(int(row["snowflake_id"]))
        assert by_guild[1234674521416663070] == [111, 222, 1326740806656069737]
        assert by_guild[999] == [333]
    finally:
        await client.close()


async def _seed_service_call_configuration_targets(database_url: str) -> None:
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    try:
        async with client.transaction() as tx:
            for guild_id in (1234674521416663070, 999):
                revision_id = uuid.uuid4()
                await tx.execute(
                    sa.text("INSERT INTO discord_guilds (discord_id, name) VALUES (:guild_id, 'QA')"),
                    {"guild_id": guild_id},
                )
                await tx.execute(
                    sa.text(
                        "INSERT INTO guild_config_revisions "
                        "(id, guild_id, revision, status, published_at) "
                        "VALUES (:id, :guild_id, 1, 'published', now())"
                    ),
                    {"id": revision_id, "guild_id": guild_id},
                )
                await tx.execute(
                    sa.text(
                        "UPDATE discord_guilds SET active_config_revision_id = :revision_id "
                        "WHERE discord_id = :guild_id"
                    ),
                    {"revision_id": revision_id, "guild_id": guild_id},
                )
                if guild_id == 1234674521416663070:
                    supervisor_policy_id = (
                        await tx.execute(
                            sa.text(
                                "SELECT id FROM guild_access_policies "
                                "WHERE config_revision_id = :revision_id "
                                "AND policy_key = 'supervisor_roles'"
                            ),
                            {"revision_id": revision_id},
                        )
                    ).scalar_one_or_none()
                    if supervisor_policy_id is None:
                        supervisor_policy_id = uuid.uuid4()
                        await tx.execute(
                            sa.text(
                                "INSERT INTO guild_access_policies "
                                "(id, config_revision_id, policy_key, subject_type, mode) "
                                "VALUES (:id, :revision_id, 'supervisor_roles', "
                                "'role', 'restricted')"
                            ),
                            {
                                "id": supervisor_policy_id,
                                "revision_id": revision_id,
                            },
                        )
                    else:
                        await tx.execute(
                            sa.text(
                                "UPDATE guild_access_policies SET mode = 'restricted' WHERE id = :policy_id"
                            ),
                            {"policy_id": supervisor_policy_id},
                        )
                    for position, role_id in enumerate((201, 202, 203)):
                        await tx.execute(
                            sa.text(
                                "INSERT INTO guild_access_policy_members "
                                "(id, policy_id, snowflake_id, position) "
                                "VALUES (:id, :policy_id, :role_id, :position)"
                            ),
                            {
                                "id": uuid.uuid4(),
                                "policy_id": supervisor_policy_id,
                                "role_id": role_id,
                                "position": position,
                            },
                        )
                    operational_policy_id = uuid.uuid4()
                    await tx.execute(
                        sa.text(
                            "INSERT INTO guild_access_policies "
                            "(id, config_revision_id, policy_key, subject_type, mode) "
                            "VALUES (:id, :revision_id, 'operational_staff_roles', "
                            "'role', 'restricted')"
                        ),
                        {
                            "id": operational_policy_id,
                            "revision_id": revision_id,
                        },
                    )
                    await tx.execute(
                        sa.text(
                            "INSERT INTO guild_access_policy_members "
                            "(id, policy_id, snowflake_id, position) "
                            "VALUES (:id, :policy_id, 201, 0)"
                        ),
                        {"id": uuid.uuid4(), "policy_id": operational_policy_id},
                    )
    finally:
        await client.close()


async def _seed_role_request_configuration_target(database_url: str) -> None:
    """Seed the LSSD feature that later FTP migrations intentionally extend."""

    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    try:
        async with client.transaction() as tx:
            revision_id = await tx.scalar(
                sa.text(
                    "SELECT id FROM guild_config_revisions "
                    "WHERE guild_id = :guild_id AND status = 'published'"
                ),
                {"guild_id": 1234674521416663070},
            )
            if revision_id is None:
                raise AssertionError("missing seeded LSSD configuration revision")
            await tx.execute(
                sa.text(
                    "INSERT INTO guild_feature_settings "
                    "(id, config_revision_id, feature_key, enabled, schema_version, payload) "
                    "VALUES (:id, :revision_id, 'role_requests', true, 1, '{}'::jsonb)"
                ),
                {"id": uuid.uuid4(), "revision_id": revision_id},
            )
            await tx.execute(
                sa.text(
                    "INSERT INTO guild_access_policies "
                    "(id, config_revision_id, policy_key, subject_type, mode) "
                    "VALUES (:id, :revision_id, 'role_request_allowed_roles', "
                    "'role', 'restricted')"
                ),
                {"id": uuid.uuid4(), "revision_id": revision_id},
            )
    finally:
        await client.close()


async def _seed_legacy_scope_refusals(database_url: str) -> int:
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    try:
        packaged = PackagedPersonaProvider()
        profiles = [
            (await packaged.load(persona_id)).profile for persona_id in ("roy_velazco", "walter_vigil")
        ]
        async with client.transaction() as tx:
            for profile in profiles:
                persona_id = uuid.uuid4()
                revision_id = uuid.uuid4()
                payload = profile.model_dump(mode="json")
                messages = dict(payload["messages"])
                access = dict(messages["access"])
                access["scope_refusal"] = f"legacy scope refusal for {profile.id}"
                messages["access"] = access
                payload["messages"] = messages
                await tx.execute(
                    sa.text("INSERT INTO personas (id, slug) VALUES (:persona_id, :slug)"),
                    {"persona_id": persona_id, "slug": profile.id},
                )
                await tx.execute(
                    sa.text(
                        "INSERT INTO persona_revisions "
                        "(id, persona_id, revision, schema_version, status, payload, checksum, "
                        "published_at) "
                        "VALUES (:revision_id, :persona_id, :revision, :schema_version, "
                        "'published', CAST(:payload AS jsonb), :checksum, now())"
                    ),
                    {
                        "revision_id": revision_id,
                        "persona_id": persona_id,
                        "revision": profile.revision,
                        "schema_version": profile.schema_version,
                        "payload": json.dumps(payload, ensure_ascii=False),
                        "checksum": _persona_payload_checksum(payload),
                    },
                )
        return len(profiles)
    finally:
        await client.close()


async def _assert_scope_refusals_removed(database_url: str, expected_count: int) -> None:
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    try:
        rows = await client.fetch_all(sa.text("SELECT payload, checksum FROM persona_revisions ORDER BY id"))
        assert len(rows) == expected_count
        for row in rows:
            payload = dict(row["payload"])
            assert "scope_refusal" not in payload["messages"]["access"]
            assert row["checksum"] == _persona_payload_checksum(payload)
            BotPersona.model_validate(payload)
    finally:
        await client.close()


def _persona_payload_checksum(payload: dict[str, object]) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(serialized).hexdigest()


async def _assert_service_call_configuration(database_url: str) -> None:
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    try:
        rows = await client.fetch_all(
            sa.text(
                "SELECT revision.guild_id, policy.mode, feature.enabled, member.snowflake_id "
                "FROM guild_config_revisions revision "
                "JOIN guild_access_policies policy "
                "ON policy.config_revision_id = revision.id "
                "AND policy.policy_key = 'service_call_channels' "
                "JOIN guild_feature_settings feature "
                "ON feature.config_revision_id = revision.id "
                "AND feature.feature_key = 'service_calls' "
                "LEFT JOIN guild_access_policy_members member ON member.policy_id = policy.id "
                "ORDER BY revision.guild_id"
            )
        )
        resolved = {int(row["guild_id"]): row for row in rows}
        lssd = resolved[1234674521416663070]
        assert lssd["mode"] == "restricted"
        assert lssd["enabled"] is True
        assert int(lssd["snowflake_id"]) == 1539037905014759485
        other = resolved[999]
        assert other["mode"] == "disabled"
        assert other["enabled"] is False
        assert other["snowflake_id"] is None
        cursor_table = await client.fetch_one(
            sa.text("SELECT to_regclass('public.service_call_cursors') AS name")
        )
        assert cursor_table is not None
        assert cursor_table["name"] == "service_call_cursors"
        faction_rows = await client.fetch_all(
            sa.text(
                "SELECT revision.guild_id, feature.enabled, feature.payload "
                "FROM guild_config_revisions revision "
                "JOIN guild_feature_settings feature "
                "ON feature.config_revision_id = revision.id "
                "AND feature.feature_key = 'faction_status' "
                "ORDER BY revision.guild_id"
            )
        )
        faction_config = {int(row["guild_id"]): row for row in faction_rows}
        assert faction_config[1234674521416663070]["enabled"] is True
        assert faction_config[1234674521416663070]["payload"] == {"faction_id": 3}
        assert faction_config[999]["enabled"] is False
        assert faction_config[999]["payload"] == {}
    finally:
        await client.close()


async def _assert_logbook_channel(database_url: str, expected_channel_id: int) -> None:
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    try:
        rows = await client.fetch_all(
            sa.text(
                "SELECT member.snowflake_id "
                "FROM guild_config_revisions revision "
                "JOIN guild_access_policies policy "
                "ON policy.config_revision_id = revision.id "
                "AND policy.policy_key = 'logbook_channels' "
                "JOIN guild_access_policy_members member ON member.policy_id = policy.id "
                "WHERE revision.guild_id = 1234674521416663070 "
                "ORDER BY revision.revision, member.position"
            )
        )
        assert [int(row["snowflake_id"]) for row in rows] == [expected_channel_id]
    finally:
        await client.close()


async def _assert_arrest_lookup_configuration(database_url: str) -> None:
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    try:
        rows = await client.fetch_all(
            sa.text(
                "SELECT revision.guild_id, feature.enabled, feature.payload "
                "FROM guild_config_revisions revision "
                "JOIN guild_feature_settings feature "
                "ON feature.config_revision_id = revision.id "
                "AND feature.feature_key = 'arrest_report_lookup' "
                "ORDER BY revision.guild_id"
            )
        )
        resolved = {int(row["guild_id"]): row for row in rows}
        assert resolved[1234674521416663070]["enabled"] is True
        assert resolved[1234674521416663070]["payload"] == {
            "faction_id": 3,
            "report_attachment_mode": "generated",
        }
        assert resolved[999]["enabled"] is False
        assert resolved[999]["payload"] == {}
        binding_table = await client.fetch_one(
            sa.text("SELECT to_regclass('public.report_arrest_bindings') AS name")
        )
        assert binding_table is not None
        assert binding_table["name"] == "report_arrest_bindings"
        structured_tables = await client.fetch_one(
            sa.text(
                "SELECT "
                "to_regclass('public.arrest_report_submissions') AS submissions, "
                "to_regclass('public.arrest_report_assets') AS assets"
            )
        )
        assert structured_tables is not None
        assert structured_tables["submissions"] == "arrest_report_submissions"
        assert structured_tables["assets"] == "arrest_report_assets"
        pending_rows = await client.fetch_all(
            sa.text(
                "SELECT revision.guild_id, feature.enabled, feature.payload "
                "FROM guild_config_revisions revision "
                "JOIN guild_feature_settings feature "
                "ON feature.config_revision_id = revision.id "
                "AND feature.feature_key = 'pending_arrest_reports' "
                "ORDER BY revision.guild_id"
            )
        )
        pending = {int(row["guild_id"]): row for row in pending_rows}
        assert pending[1234674521416663070]["enabled"] is True
        assert pending[1234674521416663070]["payload"] == {
            "faction_id": 3,
            "max_range_days": 90,
            "max_records": 1000,
        }
        assert pending[999]["enabled"] is False
        assert pending[999]["payload"] == {}
        statistics_rows = await client.fetch_all(
            sa.text(
                "SELECT revision.guild_id, feature.enabled, feature.payload "
                "FROM guild_config_revisions revision "
                "JOIN guild_feature_settings feature "
                "ON feature.config_revision_id = revision.id "
                "AND feature.feature_key = 'arrest_statistics' "
                "ORDER BY revision.guild_id"
            )
        )
        statistics = {int(row["guild_id"]): row for row in statistics_rows}
        assert statistics[1234674521416663070]["enabled"] is True
        assert statistics[1234674521416663070]["payload"] == {
            "faction_id": 3,
            "max_range_days": 30,
            "max_reports": 200,
            "cache_ttl_seconds": 900,
            "detail_concurrency": 3,
            "max_top": 10,
        }
        assert statistics[999]["enabled"] is False
        assert statistics[999]["payload"] == {}
        ftp_request_rows = await client.fetch_all(
            sa.text(
                "SELECT revision.guild_id, feature.enabled, feature.payload "
                "FROM guild_config_revisions revision "
                "JOIN guild_feature_settings feature "
                "ON feature.config_revision_id = revision.id "
                "AND feature.feature_key = 'ftp_requests' "
                "ORDER BY revision.guild_id"
            )
        )
        ftp_requests = {int(row["guild_id"]): row for row in ftp_request_rows}
        assert ftp_requests[1234674521416663070]["enabled"] is True
        assert ftp_requests[1234674521416663070]["payload"] == {
            "channel_id": 1551692208929906701,
            "additional_command_channel_ids": [1234749125820420139],
            "command_role_ids": [1234678029662294047],
            "executive_role_ids": [1234678238182244414],
        }
        assert ftp_requests[999]["enabled"] is False
        assert ftp_requests[999]["payload"] == {}
        role_request_rows = await client.fetch_all(
            sa.text(
                "SELECT revision.guild_id, feature.payload "
                "FROM guild_config_revisions revision "
                "JOIN guild_feature_settings feature "
                "ON feature.config_revision_id = revision.id "
                "AND feature.feature_key = 'role_requests' "
                "ORDER BY revision.guild_id"
            )
        )
        role_requests = {int(row["guild_id"]): row for row in role_request_rows}
        ftt_welcome = role_requests[1234674521416663070]["payload"]["ftt_entry"]["welcome"]
        assert ftt_welcome == {
            "channel_id": 1234749125820420139,
            "rules_url": "https://sheriff.sarp.es/viewtopic.php?t=37",
            "reference_channel_ids": [
                1234675521560772628,
                1467761647551774852,
            ],
        }
        warrant_notification_rows = await client.fetch_all(
            sa.text(
                "SELECT revision.guild_id, feature.enabled, feature.payload "
                "FROM guild_config_revisions revision "
                "JOIN guild_feature_settings feature "
                "ON feature.config_revision_id = revision.id "
                "AND feature.feature_key = 'warrant_notifications' "
                "ORDER BY revision.guild_id"
            )
        )
        warrant_notifications = {int(row["guild_id"]): row for row in warrant_notification_rows}
        assert warrant_notifications[1234674521416663070]["enabled"] is True
        assert warrant_notifications[1234674521416663070]["payload"] == {
            "channel_id": 1261076248541532240,
            "command_role_ids": [1234678029662294047],
            "executive_role_ids": [1234678238182244414],
        }
        assert warrant_notifications[999]["enabled"] is False
        assert warrant_notifications[999]["payload"] == {}
        ftp_request_table = await client.fetch_one(
            sa.text("SELECT to_regclass('public.ftp_training_requests') AS name")
        )
        assert ftp_request_table is not None
        assert ftp_request_table["name"] == "ftp_training_requests"
        ftp_request_columns = await client.fetch_all(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'ftp_training_requests'"
            )
        )
        assert {
            "training_phase",
            "requested_instructor_level",
            "scheduled_for",
        }.issubset({str(row["column_name"]) for row in ftp_request_columns})
        ftp_panel_table = await client.fetch_one(
            sa.text("SELECT to_regclass('public.ftp_request_panels') AS name")
        )
        assert ftp_panel_table is not None
        assert ftp_panel_table["name"] == "ftp_request_panels"
        live_member_index = await client.fetch_one(
            sa.text(
                "SELECT to_regclass('public.uq_ftp_training_request_live_member') AS name"
            )
        )
        assert live_member_index is not None
        assert live_member_index["name"] is None
        report_rows = await client.fetch_all(
            sa.text(
                "SELECT revision.guild_id, feature.enabled, feature.payload, policy.mode, "
                "member.snowflake_id "
                "FROM guild_config_revisions revision "
                "JOIN guild_feature_settings feature "
                "ON feature.config_revision_id = revision.id "
                "AND feature.feature_key = 'faction_reports' "
                "JOIN guild_access_policies policy "
                "ON policy.config_revision_id = revision.id "
                "AND policy.policy_key = 'faction_report_roles' "
                "LEFT JOIN guild_access_policy_members member ON member.policy_id = policy.id "
                "ORDER BY revision.guild_id, member.position"
            )
        )
        lssd_rows = [row for row in report_rows if int(row["guild_id"]) == 1234674521416663070]
        assert lssd_rows[0]["enabled"] is True
        assert lssd_rows[0]["payload"] == {
            "faction_id": 3,
            "max_range_days": 366,
            "max_bank_records": 10_000,
        }
        assert lssd_rows[0]["mode"] == "restricted"
        assert [int(row["snowflake_id"]) for row in lssd_rows] == [
            1234678029662294047,
            1234678238182244414,
        ]
        other_rows = [row for row in report_rows if int(row["guild_id"]) == 999]
        assert other_rows[0]["enabled"] is False
        assert other_rows[0]["payload"] == {}
        assert other_rows[0]["mode"] == "disabled"
        assert other_rows[0]["snowflake_id"] is None
        resource_rows = await client.fetch_all(
            sa.text(
                "SELECT revision.guild_id, feature.enabled, feature.payload "
                "FROM guild_config_revisions revision "
                "JOIN guild_feature_settings feature "
                "ON feature.config_revision_id = revision.id "
                "AND feature.feature_key = 'operational_resources' "
                "ORDER BY revision.guild_id"
            )
        )
        resources = {int(row["guild_id"]): row for row in resource_rows}
        assert resources[1234674521416663070]["enabled"] is True
        assert resources[1234674521416663070]["payload"]["faction_id"] == 3
        assert resources[1234674521416663070]["payload"]["game_server_address"] == ("play.sarp.es:7777")
        assert resources[999]["enabled"] is False
        assert resources[999]["payload"] == {}
        forum_rows = await client.fetch_all(
            sa.text(
                "SELECT revision.guild_id, feature.enabled, feature.payload "
                "FROM guild_config_revisions revision "
                "JOIN guild_feature_settings feature "
                "ON feature.config_revision_id = revision.id "
                "AND feature.feature_key = 'forum_shortcuts' "
                "ORDER BY revision.guild_id"
            )
        )
        forums = {int(row["guild_id"]): row for row in forum_rows}
        assert forums[1234674521416663070]["enabled"] is True
        assert forums[1234674521416663070]["payload"]["banner_url"] == ("https://i.imgur.com/gBxZZIT.gif")
        assert len(forums[1234674521416663070]["payload"]["sections"]) == 3
        assert forums[999]["enabled"] is False
        assert forums[999]["payload"] == {}
        review_rows = await client.fetch_all(
            sa.text(
                "SELECT revision.guild_id, policy.mode, member.snowflake_id "
                "FROM guild_config_revisions revision "
                "JOIN guild_access_policies policy "
                "ON policy.config_revision_id = revision.id "
                "AND policy.policy_key = 'role_request_review_channels' "
                "LEFT JOIN guild_access_policy_members member ON member.policy_id = policy.id "
                "ORDER BY revision.guild_id, member.position"
            )
        )
        lssd_review = [row for row in review_rows if int(row["guild_id"]) == 1234674521416663070]
        assert lssd_review[0]["mode"] == "restricted"
        assert [int(row["snowflake_id"]) for row in lssd_review] == [
            1261076248541532240,
        ]
        other_review = [row for row in review_rows if int(row["guild_id"]) == 999]
        assert other_review[0]["mode"] == "disabled"
        assert other_review[0]["snowflake_id"] is None
        reviewer_rows = await client.fetch_all(
            sa.text(
                "SELECT revision.guild_id, policy.mode, member.snowflake_id "
                "FROM guild_config_revisions revision "
                "JOIN guild_access_policies policy "
                "ON policy.config_revision_id = revision.id "
                "AND policy.policy_key = 'role_request_reviewer_roles' "
                "LEFT JOIN guild_access_policy_members member ON member.policy_id = policy.id "
                "ORDER BY revision.guild_id, member.position"
            )
        )
        lssd_reviewers = [row for row in reviewer_rows if int(row["guild_id"]) == 1234674521416663070]
        assert lssd_reviewers[0]["mode"] == "restricted"
        assert [int(row["snowflake_id"]) for row in lssd_reviewers] == [
            1234678029662294047,
            1234678238182244414,
        ]
        stolen_rows = await client.fetch_all(
            sa.text(
                "SELECT revision.guild_id, policy.mode, feature.enabled, feature.payload, "
                "member.snowflake_id "
                "FROM guild_config_revisions revision "
                "JOIN guild_access_policies policy "
                "ON policy.config_revision_id = revision.id "
                "AND policy.policy_key = 'stolen_vehicle_channels' "
                "JOIN guild_feature_settings feature "
                "ON feature.config_revision_id = revision.id "
                "AND feature.feature_key = 'stolen_vehicles' "
                "LEFT JOIN guild_access_policy_members member ON member.policy_id = policy.id "
                "ORDER BY revision.guild_id, member.position"
            )
        )
        stolen = {int(row["guild_id"]): row for row in stolen_rows}
        assert stolen[1234674521416663070]["mode"] == "restricted"
        assert stolen[1234674521416663070]["enabled"] is True
        assert stolen[1234674521416663070]["payload"] == {}
        assert int(stolen[1234674521416663070]["snowflake_id"]) == 1539037905014759485
        assert stolen[999]["mode"] == "disabled"
        assert stolen[999]["enabled"] is False
        assert stolen[999]["snowflake_id"] is None
        analysis_rows = await client.fetch_all(
            sa.text(
                "SELECT revision.guild_id, feature.enabled, feature.payload "
                "FROM guild_config_revisions revision "
                "JOIN guild_feature_settings feature "
                "ON feature.config_revision_id = revision.id "
                "AND feature.feature_key = 'faction_member_analysis' "
                "ORDER BY revision.guild_id"
            )
        )
        lssd_analysis = [row for row in analysis_rows if int(row["guild_id"]) == 1234674521416663070]
        assert lssd_analysis[0]["enabled"] is True
        assert lssd_analysis[0]["payload"] == {
            "faction_id": 3,
            "base_divisions": ["GNT", "DLM"],
            "inactivity_warning_days": 14,
            "inactivity_critical_days": 30,
            "inactivity_warning_percentage": 20,
            "inactivity_critical_percentage": 30,
        }
        other_analysis = [row for row in analysis_rows if int(row["guild_id"]) == 999]
        assert other_analysis[0]["enabled"] is False
        assert other_analysis[0]["payload"] == {}
        cursor_table = await client.fetch_one(
            sa.text("SELECT to_regclass('public.stolen_vehicle_feed_cursors') AS name")
        )
        assert cursor_table is not None
        assert cursor_table["name"] == "stolen_vehicle_feed_cursors"
    finally:
        await client.close()


async def _seed_pager_forward_configuration(database_url: str) -> None:
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    try:
        revisions = await client.fetch_all(
            sa.text(
                "SELECT id, guild_id FROM guild_config_revisions WHERE guild_id IN (1234674521416663070, 999)"
            )
        )
        async with client.transaction() as tx:
            for revision in revisions:
                guild_id = int(revision["guild_id"])
                await tx.execute(
                    sa.text(
                        "INSERT INTO guild_feature_settings "
                        "(id, config_revision_id, feature_key, enabled, schema_version, payload) "
                        "VALUES (:id, :revision_id, 'pagers', true, 1, CAST(:payload AS jsonb)) "
                        "ON CONFLICT (config_revision_id, feature_key) DO UPDATE "
                        "SET payload = EXCLUDED.payload"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "revision_id": revision["id"],
                        "payload": json.dumps(
                            {"starting_number": 92 if guild_id == 1234674521416663070 else 1}
                        ),
                    },
                )
    finally:
        await client.close()


async def _assert_pager_forward_configuration(
    database_url: str,
    *,
    lssd_channel_id: int | None,
) -> None:
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    try:
        rows = await client.fetch_all(
            sa.text(
                "SELECT revision.guild_id, feature.payload "
                "FROM guild_config_revisions AS revision "
                "JOIN guild_feature_settings AS feature "
                "ON feature.config_revision_id = revision.id "
                "AND feature.feature_key = 'pagers' "
                "ORDER BY revision.guild_id"
            )
        )
        payloads = {int(row["guild_id"]): row["payload"] for row in rows}
        assert payloads[1234674521416663070].get("forward_channel_id") == lssd_channel_id
        assert payloads[999].get("forward_channel_id") is None
    finally:
        await client.close()


async def _seed_deployment_channel_configuration(database_url: str) -> None:
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    try:
        revision_id = uuid.uuid4()
        policy_id = uuid.uuid4()
        async with client.transaction() as tx:
            await tx.execute(
                sa.text("INSERT INTO discord_guilds (discord_id, name) VALUES (1234674521416663070, 'LSSD')")
            )
            await tx.execute(
                sa.text(
                    "INSERT INTO guild_config_revisions "
                    "(id, guild_id, revision, status, published_at) "
                    "VALUES (:id, 1234674521416663070, 1, 'published', now())"
                ),
                {"id": revision_id},
            )
            await tx.execute(
                sa.text(
                    "UPDATE discord_guilds SET active_config_revision_id = :revision_id "
                    "WHERE discord_id = 1234674521416663070"
                ),
                {"revision_id": revision_id},
            )
            await tx.execute(
                sa.text(
                    "INSERT INTO guild_access_policies "
                    "(id, config_revision_id, policy_key, subject_type, mode) "
                    "VALUES (:id, :revision_id, 'deployment_notification_channels', "
                    "'channel', 'restricted')"
                ),
                {"id": policy_id, "revision_id": revision_id},
            )
            await tx.execute(
                sa.text(
                    "INSERT INTO guild_access_policy_members "
                    "(id, policy_id, snowflake_id, position) "
                    "VALUES (:id, :policy_id, 1241206587079524442, 0)"
                ),
                {"id": uuid.uuid4(), "policy_id": policy_id},
            )
    finally:
        await client.close()


async def _assert_deployment_channels(database_url: str, expected: tuple[int, ...]) -> None:
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    try:
        rows = await client.fetch_all(
            sa.text(
                "SELECT gapm.snowflake_id FROM discord_guilds dg "
                "JOIN guild_access_policies gap "
                "ON gap.config_revision_id = dg.active_config_revision_id "
                "JOIN guild_access_policy_members gapm ON gapm.policy_id = gap.id "
                "WHERE dg.discord_id = 1234674521416663070 "
                "AND gap.policy_key = 'deployment_notification_channels' "
                "ORDER BY gapm.snowflake_id"
            )
        )
        assert tuple(int(row["snowflake_id"]) for row in rows) == expected
    finally:
        await client.close()


async def _assert_ci_failure_channels(database_url: str, expected: tuple[int, ...]) -> None:
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    try:
        rows = await client.fetch_all(
            sa.text(
                "SELECT gapm.snowflake_id FROM discord_guilds dg "
                "JOIN guild_access_policies gap "
                "ON gap.config_revision_id = dg.active_config_revision_id "
                "JOIN guild_access_policy_members gapm ON gapm.policy_id = gap.id "
                "WHERE dg.discord_id = 1234674521416663070 "
                "AND gap.policy_key = 'ci_failure_notification_channels' "
                "ORDER BY gapm.snowflake_id"
            )
        )
        assert tuple(int(row["snowflake_id"]) for row in rows) == expected
    finally:
        await client.close()


async def _assert_deployment_incident_channels(
    database_url: str,
    expected: tuple[int, ...],
) -> None:
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    try:
        rows = await client.fetch_all(
            sa.text(
                "SELECT gapm.snowflake_id FROM discord_guilds dg "
                "JOIN guild_access_policies gap "
                "ON gap.config_revision_id = dg.active_config_revision_id "
                "JOIN guild_access_policy_members gapm ON gapm.policy_id = gap.id "
                "WHERE dg.discord_id = 1234674521416663070 "
                "AND gap.policy_key = 'deployment_incident_notification_channels' "
                "ORDER BY gapm.snowflake_id"
            )
        )
        assert tuple(int(row["snowflake_id"]) for row in rows) == expected
    finally:
        await client.close()


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
        assert persona["revision"] == 58
        assert persona["schema_version"] == 47
        global_persona = await client.fetch_one(
            sa.text(
                "SELECT persona.slug, revision.revision, revision.schema_version "
                "FROM bot_application_profile profile "
                "JOIN persona_revisions revision ON revision.id = profile.persona_revision_id "
                "JOIN personas persona ON persona.id = revision.persona_id "
                "WHERE profile.singleton_id = 1"
            )
        )
        assert global_persona is not None
        assert global_persona["slug"] == "walter_vigil"
        assert global_persona["revision"] == 58
        assert global_persona["schema_version"] == 47
        assert set(persona["payload"]["presence"]) == {
            "idle",
            "conversation",
            "knowledge",
            "personnel",
            "records",
            "evidence",
            "review",
            "response",
        }
        assert persona["payload"]["messages"]["access"]["blocked_user_messages"]
        assert persona["payload"]["messages"]["access"]["owner_quota_exhausted_messages"]
        assert persona["payload"]["messages"]["access"]["owner_invalid_report_messages"]
        assert persona["payload"]["messages"]["help"]["owner_info_start"]
        assert persona["payload"]["messages"]["help"]["owner_info_requirements"]
        assert "sin mencionarme" in persona["payload"]["messages"]["help"]["owner_info_start"]
        assert "#NÚMERO" in persona["payload"]["messages"]["help"]["owner_info_start"]
        assert persona["payload"]["messages"]["help"]["owner_info_limit"]
        assert "/info-reportes" in persona["payload"]["messages"]["help"]["feature_guides"]
        assert "/info-sugerencias" in persona["payload"]["messages"]["help"]["feature_guides"]
        assert "/guia" in persona["payload"]["messages"]["help"]["quick_references"]
        assert "general_commands" not in persona["payload"]["messages"]["help"]
        assert persona["payload"]["messages"]["interface"]["commands"]["info_reports_description"]
        assert persona["payload"]["messages"]["interface"]["callsigns"]["saved_title"]
        assert persona["payload"]["messages"]["interface"]["callsigns"]["removed_title"]
        assert "60F1" in persona["payload"]["messages"]["interface"]["callsigns"]["invalid_format"]
        assert persona["payload"]["messages"]["interface"]["callsigns"]["directory_intro"] == (
            "Personal con un callsign registrado en este servidor."
        )
        assert persona["payload"]["messages"]["interface"]["logbook"]["info_usage"]
        assert "280L" in persona["payload"]["messages"]["interface"]["callsigns"]["invalid_format"]
        assert persona["payload"]["messages"]["interface"]["deployments"]["release_intro"]
        assert persona["payload"]["messages"]["interface"]["deployments"]["ci_failed_description"]
        assert persona["payload"]["messages"]["interface"]["suggestions"]["info_warning"]
        assert persona["payload"]["messages"]["interface"]["manual_quiz"]["correct_messages"]
        assert persona["payload"]["messages"]["interface"]["manual_quiz"]["expired_messages"]
        assert "/pregunta-manual" in persona["payload"]["messages"]["help"]["quick_references"]
        assert "/info-mdc" in persona["payload"]["messages"]["help"]["feature_guides"]
        assert "/mdc" not in persona["payload"]["messages"]["help"]["quick_references"]
        assert persona["payload"]["messages"]["interface"]["mdc"]["result_intro"]
        assert persona["payload"]["messages"]["interface"]["mdc"]["info_visibility"]
        assert persona["payload"]["messages"]["interface"]["commands"]["mdc_description"]
        assert persona["payload"]["messages"]["interface"]["commands"]["info_mdc_description"]
        assert persona["payload"]["messages"]["interface"]["faction_status"]["connected_intro"]
        assert persona["payload"]["messages"]["interface"]["commands"]["connected_members_description"]
        assert "/conectados" in persona["payload"]["messages"]["help"]["quick_references"]
        assert (
            "sin PCU completo"
            in persona["payload"]["messages"]["interface"]["pending_arrest_reports"]["result_intro"]
        )
        assert (
            "revisión aprobada"
            in persona["payload"]["messages"]["interface"]["commands"]["pending_arrest_reports_description"]
        )
        assert "/reportes-pendientes" in persona["payload"]["messages"]["help"]["quick_references"]
        assert persona["payload"]["messages"]["interface"]["faction_reports"]["ready_title"]
        assert (
            "únicamente"
            not in persona["payload"]["messages"]["interface"]["faction_reports"]["ready_description"]
        )
        assert persona["payload"]["messages"]["interface"]["commands"]["faction_report_description"]
        assert "/informe-faccion" in persona["payload"]["messages"]["help"]["quick_references"]
        assert persona["payload"]["messages"]["interface"]["operational_resources"]["connected_reference"]
        assert persona["payload"]["messages"]["interface"]["commands"]["resources_description"]
        assert "/recursos" in persona["payload"]["messages"]["help"]["quick_references"]
        assert persona["payload"]["messages"]["interface"]["stolen_vehicles"]["no_results"]
        assert persona["payload"]["messages"]["interface"]["commands"]["stolen_vehicles_description"]
        assert "/vehiculos-robados" in persona["payload"]["messages"]["help"]["quick_references"]
        assert persona["payload"]["messages"]["interface"]["faction_member_analysis"]["intro"]
        assert persona["payload"]["messages"]["interface"]["commands"]["faction_member_analysis_description"]
        assert persona["payload"]["messages"]["interface"]["ftp_requests"]["published"]
        assert "{channel}" in persona["payload"]["messages"]["interface"]["ftp_requests"]["wrong_channel"]
        assert persona["payload"]["messages"]["interface"]["commands"]["ftp_request_description"]
        assert persona["payload"]["messages"]["interface"]["commands"]["info_ftp_description"]
        assert "/solicitar-ficha" in persona["payload"]["messages"]["help"]["quick_references"]
        assert persona["payload"]["messages"]["interface"]["commands"][
            "active_ftp_requests_description"
        ]
        assert "/fichas-activas" in persona["payload"]["messages"]["help"]["quick_references"]
        analysis_feature = await client.fetch_one(
            sa.text(
                "SELECT enabled, payload FROM guild_feature_settings "
                "WHERE config_revision_id = :revision_id "
                "AND feature_key = 'faction_member_analysis'"
            ),
            {"revision_id": identifiers["config_revision_id"]},
        )
        assert analysis_feature is not None
        assert analysis_feature["enabled"] is False
        assert analysis_feature["payload"] == {}
        resource_feature = await client.fetch_one(
            sa.text(
                "SELECT enabled, payload FROM guild_feature_settings "
                "WHERE config_revision_id = :revision_id "
                "AND feature_key = 'operational_resources'"
            ),
            {"revision_id": identifiers["config_revision_id"]},
        )
        assert resource_feature is not None
        assert resource_feature["enabled"] is False
        assert resource_feature["payload"] == {}
        assert persona["payload"]["messages"]["interface"]["commands"]["teamspeak_unauthorized"]
        table = await client.fetch_one(sa.text("SELECT to_regclass('public.guild_member_profiles') AS name"))
        assert table is not None and table["name"] == "guild_member_profiles"
    finally:
        await client.close()


async def _set_global_persona_revision(
    database_url: str,
    slug: str,
    revision: int,
) -> None:
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    try:
        await client.execute(
            sa.text(
                "UPDATE bot_application_profile profile "
                "SET persona_revision_id = revision.id "
                "FROM personas persona "
                "JOIN persona_revisions revision ON revision.persona_id = persona.id "
                "WHERE profile.singleton_id = 1 "
                "AND persona.slug = :slug AND revision.revision = :revision"
            ),
            {"slug": slug, "revision": revision},
        )
    finally:
        await client.close()


async def _assert_global_persona_revision(
    database_url: str,
    slug: str,
    revision: int,
    schema_version: int,
) -> None:
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    try:
        row = await client.fetch_one(
            sa.text(
                "SELECT persona.slug, revision.revision, revision.schema_version, "
                "revision.payload "
                "FROM bot_application_profile profile "
                "JOIN persona_revisions revision ON revision.id = profile.persona_revision_id "
                "JOIN personas persona ON persona.id = revision.persona_id "
                "WHERE profile.singleton_id = 1"
            )
        )
        assert row is not None
        assert row["slug"] == slug
        assert row["revision"] == revision
        assert row["schema_version"] == schema_version
        if schema_version >= 45:
            assert row["payload"]["messages"]["interface"]["faction_member_analysis"]
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
        ci_repository = CiNotificationRepository(client)
        ci_event = GitHubWorkflowRunEvent.model_validate(
            {
                "action": "completed",
                "repository": {
                    "id": 123456,
                    "full_name": "SouthViking/arrest-report-agent",
                },
                "sender": {"login": "SouthViking"},
                "workflow_run": {
                    "id": 31741783031,
                    "workflow_id": 9001,
                    "name": "CI",
                    "event": "push",
                    "status": "completed",
                    "conclusion": "failure",
                    "run_attempt": 1,
                    "head_branch": "any-branch",
                    "head_sha": "b" * 40,
                    "actor": {"login": "SouthViking"},
                    "head_commit": {"message": "Break CI"},
                    "updated_at": datetime.now(UTC).isoformat(),
                },
            }
        )
        ci_record, ci_inserted = await ci_repository.record_event("delivery-1", ci_event)
        ci_duplicate, ci_inserted_again = await ci_repository.record_event(
            "delivery-redelivery",
            ci_event,
        )
        assert ci_inserted is True
        assert ci_inserted_again is False
        assert ci_duplicate.id == ci_record.id
        assert (
            await ci_repository.enqueue_deliveries(
                ci_record.id,
                (
                    CiDeliveryTarget(
                        guild_id=123,
                        channel_id=456,
                        notification_key="ci-failure:123:456:123456:31741783031:1",
                    ),
                ),
            )
            == 1
        )
        ci_deliveries = await ci_repository.due_deliveries()
        assert len(ci_deliveries) == 1
        assert ci_deliveries[0].event.branch == "any-branch"
        await ci_repository.mark_delivery_sent(ci_deliveries[0].id, 790)
        assert await ci_repository.due_deliveries() == ()
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

        session_manager = AgentSessionManager(client)
        conversation_key = AgentConversationKey(
            namespace="discord_agent_thread",
            scope_key="guild:123:root:456",
            access_partition="full",
        )
        first_session = await session_manager.prepare(
            conversation_key,
            next_source_cursor="100",
        )
        assert first_session.created
        assert first_session.source_cursor is None
        await first_session.session.add_items(  # type: ignore[arg-type]
            [{"role": "user", "content": "primera consulta"}]
        )
        resumed_session = await session_manager.prepare(
            conversation_key,
            next_source_cursor="200",
        )
        assert not resumed_session.created
        assert resumed_session.source_cursor == "100"
        assert await resumed_session.session.get_items() == [{"role": "user", "content": "primera consulta"}]
    finally:
        await client.close()
