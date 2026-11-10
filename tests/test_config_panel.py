from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import discord
import pytest

from arrest_report_agent.config import (
    AccessPolicyKey,
    AccessPolicyMode,
    AccessPolicySnapshot,
    GuildAgentConfig,
    ModelCatalog,
)
from arrest_report_agent.cost_monitoring import (
    CostMonitoringSnapshot,
    OpenAICostSnapshot,
)
from arrest_report_agent.infrastructure.discord.config_admin import (
    AccessPolicyModal,
    AgentConfigurationModal,
    BlacklistConfigurationModal,
    ConfigurationAdminCog,
    ConfigurationPanelView,
    DraftConfigurationModal,
    FeatureConfigurationModal,
    PersonaConfigurationModal,
    PublishConfigurationModal,
    RollbackConfigurationModal,
    build_config_panel_embed,
    build_config_unauthorized_embed,
)


class FakeRepository:
    def __init__(self) -> None:
        self.created: list[dict[str, int]] = []

    async def draft_revision_id(self, _guild_id: int):
        return None

    async def create_draft(
        self,
        guild_id: int,
        *,
        actor_user_id: int,
        interaction_id: int,
    ) -> UUID:
        self.created.append(
            {
                "guild_id": guild_id,
                "actor_user_id": actor_user_id,
                "interaction_id": interaction_id,
            }
        )
        return UUID("00000000-0000-0000-0000-000000000123")


class FakeProvider:
    def __init__(self, persona, agent: GuildAgentConfig) -> None:
        report_policy = AccessPolicySnapshot(
            key=AccessPolicyKey.REPORT_CHANNELS,
            subject_type="channel",
            mode=AccessPolicyMode.RESTRICTED,
            ids=(300,),
        )
        admin_policy = AccessPolicySnapshot(
            key=AccessPolicyKey.CONFIG_ADMIN_ROLES,
            subject_type="role",
            mode=AccessPolicyMode.RESTRICTED,
            ids=(700,),
        )
        self.snapshot = SimpleNamespace(
            revision=4,
            persona_id="roy_velazco",
            persona_revision=24,
            agent=agent,
            policies={
                AccessPolicyKey.REPORT_CHANNELS: report_policy,
                AccessPolicyKey.CONFIG_ADMIN_ROLES: admin_policy,
            },
        )
        self.snapshot.policy = self.snapshot.policies.__getitem__
        self.persona = persona
        self.model_catalog = ModelCatalog.load_default()

    async def cached(self, _guild_id: int):
        return SimpleNamespace(
            snapshot=self.snapshot,
            persona=SimpleNamespace(profile=self.persona),
        )


class FakeContext:
    def __init__(self, *, user_id: int) -> None:
        self.guild = SimpleNamespace(id=100)
        self.author = SimpleNamespace(id=user_id)
        self.interaction = SimpleNamespace(id=500)
        self.responses: list[dict[str, object]] = []

    async def respond(self, **kwargs: object) -> None:
        self.responses.append(kwargs)


class FakeInteractionResponse:
    def __init__(self) -> None:
        self.done = False

    def is_done(self) -> bool:
        return self.done

    async def defer(self, *, ephemeral: bool = False, invisible: bool = True) -> None:
        self.ephemeral = ephemeral
        self.invisible = invisible
        self.done = True


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send(self, **kwargs: object) -> None:
        self.messages.append(kwargs)


class FakeInteraction:
    def __init__(self, *, user_id: int, interaction_id: int = 600) -> None:
        self.id = interaction_id
        self.data: dict[str, object] = {}
        self.guild = SimpleNamespace(id=100)
        self.user = SimpleNamespace(id=user_id)
        self.response = FakeInteractionResponse()
        self.followup = FakeFollowup()
        self.channel = FakeChannel()
        self.channel_id = 300


class FakeChannel:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send(self, **kwargs: object) -> None:
        self.messages.append(kwargs)


class FakeCostMonitoring:
    async def snapshot(self) -> CostMonitoringSnapshot:
        return CostMonitoringSnapshot(
            fetched_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
            openai=OpenAICostSnapshot(
                current_spend=Decimal("1.25"),
                hard_limit=Decimal("10"),
                remaining=Decimal("8.75"),
                limit_scope="project",
            ),
        )


def agent_config(settings) -> GuildAgentConfig:
    return GuildAgentConfig(
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
        thread_auto_archive_minutes=settings.thread_auto_archive_minutes,
    )


def test_config_is_one_root_command_without_user_arguments() -> None:
    command = ConfigurationAdminCog.config

    assert command.name == "config"
    assert command.parent is None
    assert list(inspect.signature(command.callback).parameters) == ["self", "ctx"]


def test_unauthorized_embed_uses_the_persona_message_pool(persona) -> None:
    first = build_config_unauthorized_embed(persona, 0)
    second = build_config_unauthorized_embed(persona, 1)

    assert first.title == "🔒 Configuración restringida"
    assert persona.messages.access.unauthorized_user_intros[0] in first.description
    assert persona.messages.access.unauthorized_user_intros[1] in second.description
    assert first.description != second.description


@pytest.mark.asyncio
async def test_unauthorized_config_command_is_ephemeral(settings, persona) -> None:
    provider = FakeProvider(persona, agent_config(settings))
    cog = ConfigurationAdminCog(
        settings,
        FakeRepository(),  # type: ignore[arg-type]
        provider,  # type: ignore[arg-type]
    )
    context = FakeContext(user_id=999)

    await ConfigurationAdminCog.config.callback(cog, context)  # type: ignore[arg-type]

    assert len(context.responses) == 1
    assert context.responses[0]["ephemeral"] is True
    embed = context.responses[0]["embed"]
    assert isinstance(embed, discord.Embed)
    assert embed.title == "🔒 Configuración restringida"


@pytest.mark.asyncio
async def test_owner_opens_an_ephemeral_configuration_panel(settings, persona) -> None:
    owner_settings = settings.model_copy(update={"config_owner_user_ids_csv": "42"})
    provider = FakeProvider(persona, agent_config(settings))
    cog = ConfigurationAdminCog(
        owner_settings,
        FakeRepository(),  # type: ignore[arg-type]
        provider,  # type: ignore[arg-type]
    )
    context = FakeContext(user_id=42)

    await ConfigurationAdminCog.config.callback(cog, context)  # type: ignore[arg-type]

    assert len(context.responses) == 1
    response = context.responses[0]
    assert response["ephemeral"] is True
    assert isinstance(response["view"], ConfigurationPanelView)
    embed = response["embed"]
    assert isinstance(embed, discord.Embed)
    assert embed.title == "⚙️ Panel de configuración"
    assert "roy_velazco" in embed.fields[0].value


@pytest.mark.asyncio
@pytest.mark.parametrize(("whitelist", "user_id", "authorized"), [
    ("42,43", 42, True), ("42,43", 43, True), ("42,43", 99, False),
    ("43", 42, False), ("43", 43, True), ("", 449044161954447360, False),
])
async def test_config_access_uses_the_operator_whitelist_after_handoff(
    settings, persona, whitelist, user_id, authorized,
) -> None:
    configured = settings.model_copy(update={"config_owner_user_ids_csv": whitelist})
    provider = FakeProvider(persona, agent_config(settings))
    cog = ConfigurationAdminCog(configured, FakeRepository(), provider)
    ctx = FakeContext(user_id=user_id)
    await ConfigurationAdminCog.config.callback(cog, ctx)
    response = ctx.responses[0]
    assert response["ephemeral"] is True
    assert isinstance(response.get("view"), ConfigurationPanelView) is authorized


@pytest.mark.asyncio
async def test_draft_modal_revalidates_owner_and_uses_interaction_id(settings, persona) -> None:
    owner_settings = settings.model_copy(update={"config_owner_user_ids_csv": "42"})
    repository = FakeRepository()
    provider = FakeProvider(persona, agent_config(settings))
    cog = ConfigurationAdminCog(
        owner_settings,
        repository,  # type: ignore[arg-type]
        provider,  # type: ignore[arg-type]
    )
    modal = DraftConfigurationModal(cog, 42)
    modal.action._selected_values = ["create"]  # noqa: SLF001
    interaction = FakeInteraction(user_id=42, interaction_id=601)
    modal.action._interaction = interaction  # type: ignore[attr-defined]  # noqa: SLF001

    await modal.callback(interaction)  # type: ignore[arg-type]

    assert repository.created == [{"guild_id": 100, "actor_user_id": 42, "interaction_id": 601}]
    assert interaction.response.done is True
    assert interaction.followup.messages[0]["ephemeral"] is True


@pytest.mark.asyncio
async def test_panel_exposes_forms_instead_of_inline_arguments(settings, persona) -> None:
    owner_settings = settings.model_copy(update={"config_owner_user_ids_csv": "42"})
    provider = FakeProvider(persona, agent_config(settings))
    cog = ConfigurationAdminCog(
        owner_settings,
        FakeRepository(),  # type: ignore[arg-type]
        provider,  # type: ignore[arg-type]
    )
    panel = ConfigurationPanelView(cog, requester_id=42)
    labels = [child.label for child in panel.children]

    assert labels == [
        "Actualizar",
        "Borrador",
        "Publicar",
        "Accesos",
        "Agente",
        "Módulos",
        "Personas",
        "Blacklists",
        "Rollback",
    ]
    assert DraftConfigurationModal(cog, 42).title == "Administrar borrador"
    assert PublishConfigurationModal(cog, 42).title == "Publicar configuración"
    access_modal = AccessPolicyModal(cog, 42)
    assert access_modal.title == "Configurar accesos"
    assert AccessPolicyKey.AGENT_BLOCKED_USERS.value in {
        option.value for option in access_modal.policy.options
    }
    assert AccessPolicyKey.CI_FAILURE_NOTIFICATION_CHANNELS.value in {
        option.value for option in access_modal.policy.options
    }
    assert AccessPolicyKey.DEPLOYMENT_INCIDENT_NOTIFICATION_CHANNELS.value in {
        option.value for option in access_modal.policy.options
    }
    assert AccessPolicyKey.SERVICE_CALL_CHANNELS.value in {
        option.value for option in access_modal.policy.options
    }
    assert AccessPolicyKey.RADIO_AUDIT_ROLES.value in {
        option.value for option in access_modal.policy.options
    }
    assert len(access_modal.policy.options) <= 25
    assert AgentConfigurationModal(cog, 42, provider.snapshot.agent).title == "Configurar agente"
    feature_modal = FeatureConfigurationModal(cog, 42)
    assert feature_modal.title == "Configurar módulo"
    assert "deployment_notifications" in {
        option.value for option in feature_modal.key.options
    }
    assert "service_calls" in {
        option.value for option in feature_modal.key.options
    }
    assert "faction_status" in {
        option.value for option in feature_modal.key.options
    }
    assert "faction_member_analysis" in {
        option.value for option in feature_modal.key.options
    }
    assert "operational_resources" in {
        option.value for option in feature_modal.key.options
    }
    assert "forum_shortcuts" in {
        option.value for option in feature_modal.key.options
    }
    assert "pending_arrest_reports" in {
        option.value for option in feature_modal.key.options
    }
    assert "radio_audit" in {option.value for option in feature_modal.key.options}
    assert "fleet" in {option.value for option in feature_modal.key.options}
    assert len(feature_modal.key.options) <= 25
    assert "warrant_notifications" in {
        option.value for option in feature_modal.key.options
    }
    blacklist_modal = BlacklistConfigurationModal(cog, 42)
    assert blacklist_modal.title == "Configurar blacklists"
    assert {option.value for option in blacklist_modal.action.options} == {
        "upsert",
        "disable",
        "roles",
    }
    assert blacklist_modal.key.required is False
    persona_modal = PersonaConfigurationModal(cog, 42, include_global_profile=True)
    assert persona_modal.title == "Administrar personas"
    assert [option.value for option in persona_modal.action.options][-1] == "global"
    assert RollbackConfigurationModal(cog, 42).title == "Restaurar configuración"


def test_panel_splits_long_policy_summaries_at_discord_field_limit(settings) -> None:
    snapshot = SimpleNamespace(
        revision=4,
        persona_id="roy_velazco",
        persona_revision=57,
        agent=agent_config(settings),
        policies={
            key: SimpleNamespace(mode=AccessPolicyMode.DISABLED, ids=())
            for key in AccessPolicyKey
        },
        features={},
    )

    embed = build_config_panel_embed(snapshot, draft_id=None)
    policy_fields = [field for field in embed.fields if field.name.startswith("Políticas")]

    assert len(policy_fields) > 1
    assert all(len(field.value) <= 1024 for field in embed.fields)
    rendered = "\n".join(field.value for field in policy_fields)
    assert "Canales de bitácora" in rendered


@pytest.mark.asyncio
async def test_owner_can_publish_cost_monitoring_from_the_private_panel(settings, persona) -> None:
    owner_settings = settings.model_copy(update={"config_owner_user_ids_csv": "42"})
    provider = FakeProvider(persona, agent_config(settings))
    cog = ConfigurationAdminCog(
        owner_settings,
        FakeRepository(),  # type: ignore[arg-type]
        provider,  # type: ignore[arg-type]
        FakeCostMonitoring(),  # type: ignore[arg-type]
    )
    panel = ConfigurationPanelView(cog, requester_id=42)
    button = next(child for child in panel.children if child.label == "Publicar consumo")
    interaction = FakeInteraction(user_id=42)

    await button.callback(interaction)  # type: ignore[arg-type]

    assert interaction.response.done is True
    assert interaction.response.invisible is True
    assert interaction.response.ephemeral is False
    assert len(interaction.channel.messages) == 1
    message = interaction.channel.messages[0]
    allowed_mentions = message["allowed_mentions"]
    assert isinstance(allowed_mentions, discord.AllowedMentions)
    assert allowed_mentions.everyone is False
    assert allowed_mentions.users is False
    assert allowed_mentions.roles is False
    embed = message["embed"]
    assert isinstance(embed, discord.Embed)
    assert embed.title == "💰 Estado de consumo"


@pytest.mark.asyncio
async def test_cost_monitoring_action_is_hidden_from_non_owner_config_admins(
    settings,
    persona,
) -> None:
    owner_settings = settings.model_copy(update={"config_owner_user_ids_csv": "42"})
    cog = ConfigurationAdminCog(
        owner_settings,
        FakeRepository(),  # type: ignore[arg-type]
        FakeProvider(persona, agent_config(settings)),  # type: ignore[arg-type]
        FakeCostMonitoring(),  # type: ignore[arg-type]
    )

    panel = ConfigurationPanelView(cog, requester_id=99)

    assert "Publicar consumo" not in [child.label for child in panel.children]
