"""Private Discord control panel for versioned PostgreSQL configuration."""

from __future__ import annotations

import json
import logging
import mimetypes
from collections.abc import Awaitable, Callable

import discord
from discord.ext import commands

from arrest_report_agent.config import (
    AccessPolicyKey,
    AccessPolicyMode,
    AccessPolicySnapshot,
    AppSettings,
    BotPersona,
    GuildAgentConfig,
    GuildFeatureSnapshot,
)
from arrest_report_agent.config.database import POLICY_SUBJECT_TYPES
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.cost_monitoring import (
    CostMonitoringService,
    build_cost_monitoring_embed,
)
from arrest_report_agent.infrastructure.postgres import (
    ConfigurationRepository,
    DatabaseConfigProvider,
)

LOGGER = logging.getLogger(__name__)

POLICY_LABELS: dict[AccessPolicyKey, str] = {
    AccessPolicyKey.REPORT_CHANNELS: "Canales del agente",
    AccessPolicyKey.AGENT_USERS: "Usuarios del agente",
    AccessPolicyKey.AGENT_ROLES: "Roles con acceso al agente",
    AccessPolicyKey.AGENT_BLOCKED_USERS: "Usuarios bloqueados del agente",
    AccessPolicyKey.SUPERVISOR_ROLES: "Roles de supervisión",
    AccessPolicyKey.REPORT_ANALYTICS_ROLES: "Roles de analítica de reportes",
    AccessPolicyKey.REVIEW_ESCALATION_ROLES: "Roles de derivación",
    AccessPolicyKey.ROLE_REQUEST_CHANNELS: "Canales de solicitudes de roles",
    AccessPolicyKey.ROLE_REQUEST_ALLOWED_ROLES: "Roles solicitables",
    AccessPolicyKey.ROLE_REQUEST_REVIEWER_ROLES: "Revisores de roles",
    AccessPolicyKey.CONFIG_ADMIN_ROLES: "Administradores de configuración",
    AccessPolicyKey.PAGER_CHANNELS: "Canales de pagers",
    AccessPolicyKey.PAGER_CREATOR_ROLES: "Emisores de pagers",
    AccessPolicyKey.PAGER_RECIPIENT_ROLES: "Destinatarios de pagers",
    AccessPolicyKey.PAGER_CLOSER_ROLES: "Responsables de cierre de pagers",
    AccessPolicyKey.OPERATIONAL_STAFF_ROLES: "Personal operativo con callsign",
    AccessPolicyKey.DEPLOYMENT_NOTIFICATION_CHANNELS: "Canales de despliegues",
    AccessPolicyKey.DEPLOYMENT_INCIDENT_NOTIFICATION_CHANNELS: (
        "Canales de incidentes de despliegue"
    ),
    AccessPolicyKey.CI_FAILURE_NOTIFICATION_CHANNELS: "Canales de errores de CI",
    AccessPolicyKey.SERVICE_CALL_CHANNELS: "Canales de llamadas de servicio",
}
FEATURE_LABELS = {
    "role_requests": "Solicitudes de roles",
    "pagers": "Pagers",
    "teamspeak": "TeamSpeak",
    "deployment_notifications": "Notificaciones de despliegue",
    "service_calls": "Llamadas de servicio",
    "faction_status": "Estado de facción",
    "arrest_report_lookup": "Consulta de reportes de arresto",
    "pending_arrest_reports": "Reportes de arresto pendientes",
}
MODE_LABELS = {
    AccessPolicyMode.OPEN: "abierta",
    AccessPolicyMode.RESTRICTED: "restringida",
    AccessPolicyMode.DISABLED: "deshabilitada",
}
CONFIG_PANEL_TIMEOUT_SECONDS = 900
MAX_PERSONA_JSON_BYTES = 1_000_000
MAX_PERSONA_AVATAR_BYTES = 10_485_760

MutationResult = tuple[str, str, discord.Color]
Mutation = Callable[[], Awaitable[MutationResult]]


def build_config_unauthorized_embed(persona: BotPersona, interaction_id: int) -> discord.Embed:
    """Build a personality-aware denial from the active persona's message pool."""

    messages = persona.messages.access
    intro = messages.unauthorized_user_intros[interaction_id % len(messages.unauthorized_user_intros)]
    description = messages.unauthorized_user.format(
        form_of_address=persona.identity.default_form_of_address,
        intro=intro,
    )
    return discord.Embed(
        title="🔒 Configuración restringida",
        description=description,
        color=discord.Color.red(),
    )


def build_config_panel_embed(snapshot: object, draft_id: object | None) -> discord.Embed:
    """Render the current immutable configuration and draft status."""

    policies = "\n".join(
        f"• **{POLICY_LABELS[key]}:** {MODE_LABELS[policy.mode]} ({len(policy.ids)})"
        for key, policy in sorted(snapshot.policies.items(), key=lambda item: item[0].value)
    )
    features = (
        "\n".join(
            f"• **{FEATURE_LABELS.get(key, key)}:** {'habilitado' if feature.enabled else 'deshabilitado'}"
            for key, feature in sorted(getattr(snapshot, "features", {}).items())
        )
        or "No hay módulos configurados."
    )
    embed = discord.Embed(
        title="⚙️ Panel de configuración",
        description=(
            "Administra un borrador y publícalo cuando esté listo. "
            "Los cambios activos se actualizan sin redesplegar el bot."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Configuración activa",
        value=(
            f"**Revisión:** {snapshot.revision}\n"
            f"**Persona:** `{snapshot.persona_id}` r{snapshot.persona_revision}\n"
            f"**Modelo:** `{snapshot.agent.model}` · `{snapshot.agent.reasoning_effort}`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Borrador",
        value=f"`{draft_id}`" if draft_id is not None else "No hay un borrador abierto.",
        inline=False,
    )
    embed.add_field(name="Políticas", value=policies, inline=False)
    embed.add_field(name="Módulos", value=features, inline=False)
    embed.set_footer(text="Panel privado · expira tras 15 minutos")
    return embed


class ConfigurationAdminCog(commands.Cog):
    """Expose one argument-free slash command backed by private forms."""

    def __init__(
        self,
        settings: AppSettings,
        repository: ConfigurationRepository,
        provider: DatabaseConfigProvider,
        cost_monitoring: CostMonitoringService | None = None,
    ) -> None:
        self.repository = repository
        self.provider = provider
        self.cost_monitoring = cost_monitoring
        self.owner_ids = frozenset(settings.config_owner_user_ids)

    async def _persona(self, guild_id: int) -> BotPersona:
        return (await self.provider.cached(guild_id)).persona.profile

    async def _is_authorized(
        self,
        guild: discord.Guild | None,
        member: discord.abc.User,
        *,
        owner_only: bool = False,
    ) -> bool:
        if member.id in self.owner_ids:
            return True
        if owner_only or guild is None or not isinstance(member, discord.Member):
            return False
        bundle = await self.provider.cached(guild.id)
        policy = bundle.snapshot.policy(AccessPolicyKey.CONFIG_ADMIN_ROLES)
        return policy.mode is AccessPolicyMode.RESTRICTED and any(
            role.id in policy.ids for role in member.roles
        )

    async def _unauthorized_embed(
        self,
        guild: discord.Guild | None,
        interaction_id: int,
    ) -> discord.Embed:
        if guild is None:
            return discord.Embed(
                title="🔒 Configuración restringida",
                description="Este panel solo está disponible dentro de un servidor configurado.",
                color=discord.Color.red(),
            )
        return build_config_unauthorized_embed(await self._persona(guild.id), interaction_id)

    async def authorize_context(
        self,
        ctx: discord.ApplicationContext,
        *,
        owner_only: bool = False,
    ) -> bool:
        try:
            authorized = await self._is_authorized(ctx.guild, ctx.author, owner_only=owner_only)
        except Exception as error:
            await self._context_failure(ctx, error, operation="authorize")
            return False
        if authorized:
            return True
        await ctx.respond(
            embed=await self._unauthorized_embed(ctx.guild, ctx.interaction.id),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    async def authorize_interaction(
        self,
        interaction: discord.Interaction,
        requester_id: int,
        *,
        owner_only: bool = False,
    ) -> bool:
        if interaction.user.id != requester_id:
            await self._send_interaction_embed(
                interaction,
                discord.Embed(
                    title="🔒 Panel privado",
                    description="Este panel pertenece a otra sesión de administración.",
                    color=discord.Color.red(),
                ),
            )
            return False
        try:
            authorized = await self._is_authorized(
                interaction.guild,
                interaction.user,
                owner_only=owner_only,
            )
        except Exception as error:
            await self._interaction_failure(interaction, error, operation="authorize")
            return False
        if authorized:
            return True
        await self._send_interaction_embed(
            interaction,
            await self._unauthorized_embed(interaction.guild, interaction.id),
        )
        return False

    @staticmethod
    async def _send_interaction_embed(
        interaction: discord.Interaction,
        embed: discord.Embed,
    ) -> None:
        kwargs = {
            "embed": embed,
            "ephemeral": True,
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if interaction.response.is_done():
            await interaction.followup.send(**kwargs)
        else:
            await interaction.response.send_message(**kwargs)

    async def _context_failure(
        self,
        ctx: discord.ApplicationContext,
        error: Exception,
        *,
        operation: str,
    ) -> None:
        self._log_failure(
            error,
            operation=operation,
            guild_id=ctx.guild.id if ctx.guild else None,
            user_id=ctx.author.id,
            interaction_id=ctx.interaction.id,
        )
        await ctx.respond(
            embed=self._error_embed(error, ctx.interaction.id),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _interaction_failure(
        self,
        interaction: discord.Interaction,
        error: Exception,
        *,
        operation: str,
    ) -> None:
        self._log_failure(
            error,
            operation=operation,
            guild_id=interaction.guild.id if interaction.guild else None,
            user_id=interaction.user.id,
            interaction_id=interaction.id,
        )
        await self._send_interaction_embed(
            interaction,
            self._error_embed(error, interaction.id),
        )

    @staticmethod
    def _log_failure(
        error: Exception,
        *,
        operation: str,
        guild_id: int | None,
        user_id: int,
        interaction_id: int,
    ) -> None:
        log_event(
            LOGGER,
            "error",
            "database_configuration_command_failed",
            exc_info=error,
            operation=operation,
            guild_id=guild_id,
            user_id=user_id,
            interaction_id=interaction_id,
            **exception_fields(error),
        )

    @staticmethod
    def _error_embed(error: Exception, interaction_id: int) -> discord.Embed:
        if isinstance(error, (LookupError, ValueError, json.JSONDecodeError)):
            description = str(error) or "No pude aplicar ese cambio."
        else:
            description = f"No pude aplicar ese cambio. Referencia: `{interaction_id}`."
        return discord.Embed(
            title="⚠️ Configuración",
            description=description,
            color=discord.Color.red(),
        )

    async def run_mutation(
        self,
        interaction: discord.Interaction,
        requester_id: int,
        operation: str,
        mutation: Mutation,
        *,
        owner_only: bool = False,
    ) -> None:
        if not await self.authorize_interaction(
            interaction,
            requester_id,
            owner_only=owner_only,
        ):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            title, description, color = await mutation()
        except Exception as error:
            await self._interaction_failure(interaction, error, operation=operation)
            return
        await self._send_interaction_embed(
            interaction,
            discord.Embed(title=title, description=description, color=color),
        )
        log_event(
            LOGGER,
            "info",
            "database_configuration_command_completed",
            operation=operation,
            guild_id=interaction.guild.id if interaction.guild else None,
            user_id=interaction.user.id,
            interaction_id=interaction.id,
        )

    async def panel_embed(self, guild_id: int) -> discord.Embed:
        snapshot = (await self.provider.cached(guild_id)).snapshot
        draft_id = await self.repository.draft_revision_id(guild_id)
        return build_config_panel_embed(snapshot, draft_id)

    async def publish_cost_monitoring(
        self,
        interaction: discord.Interaction,
        requester_id: int,
    ) -> None:
        """Publish an owner-triggered provider summary in the current channel."""

        if not await self.authorize_interaction(
            interaction,
            requester_id,
            owner_only=True,
        ):
            return
        if self.cost_monitoring is None:
            await self._send_interaction_embed(
                interaction,
                discord.Embed(
                    title="⚠️ Consumo no configurado",
                    description="No hay proveedores de consumo configurados en este proceso.",
                    color=discord.Color.red(),
                ),
            )
            return
        await interaction.response.defer(invisible=True)
        try:
            snapshot = await self.cost_monitoring.snapshot()
            channel = interaction.channel
            if channel is None:
                raise LookupError("No pude resolver el canal donde publicar el consumo.")
            await channel.send(
                embed=build_cost_monitoring_embed(snapshot),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as error:
            await self._interaction_failure(interaction, error, operation="costs.publish")
            return
        log_event(
            LOGGER,
            "info",
            "cost_monitoring_published",
            guild_id=interaction.guild.id if interaction.guild else None,
            channel_id=interaction.channel_id,
            user_id=interaction.user.id,
            interaction_id=interaction.id,
            openai_included=snapshot.openai is not None,
            railway_included=snapshot.railway is not None,
        )

    async def draft_policy(
        self,
        guild_id: int,
        key: AccessPolicyKey,
    ) -> AccessPolicySnapshot:
        draft_id = await self.repository.draft_revision_id(guild_id)
        if draft_id is None:
            raise LookupError("Crea un borrador antes de editar accesos.")
        snapshot = await self.repository.snapshot_for_revision(guild_id, draft_id)
        return snapshot.policy(key)

    @discord.slash_command(
        name="config",
        description="Abre el panel privado de configuración",
        contexts={discord.InteractionContextType.guild},
    )
    async def config(self, ctx: discord.ApplicationContext) -> None:
        if not await self.authorize_context(ctx) or ctx.guild is None:
            return
        try:
            embed = await self.panel_embed(ctx.guild.id)
        except Exception as error:
            await self._context_failure(ctx, error, operation="show")
            return
        await ctx.respond(
            embed=embed,
            view=ConfigurationPanelView(self, requester_id=ctx.author.id),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class ConfigurationPanelView(discord.ui.View):
    """Requester-bound navigation for the private configuration panel."""

    def __init__(self, cog: ConfigurationAdminCog, *, requester_id: int) -> None:
        super().__init__(timeout=CONFIG_PANEL_TIMEOUT_SECONDS, disable_on_timeout=True)
        self.cog = cog
        self.requester_id = requester_id
        if requester_id not in cog.owner_ids or cog.cost_monitoring is None:
            consumption = discord.utils.get(
                self.children,
                custom_id="config:v2:cost-monitoring",
            )
            if consumption is not None:
                self.remove_item(consumption)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.cog.authorize_interaction(interaction, self.requester_id)

    @discord.ui.button(label="Actualizar", emoji="🔄", style=discord.ButtonStyle.secondary, row=0)
    async def refresh(
        self,
        _button: discord.ui.Button,
        interaction: discord.Interaction,
    ) -> None:
        if interaction.guild is None:
            return
        try:
            embed = await self.cog.panel_embed(interaction.guild.id)
        except Exception as error:
            await self.cog._interaction_failure(interaction, error, operation="show")
            return
        await interaction.response.edit_message(
            embed=embed,
            view=ConfigurationPanelView(self.cog, requester_id=self.requester_id),
        )

    @discord.ui.button(label="Borrador", emoji="📝", style=discord.ButtonStyle.primary, row=0)
    async def draft(
        self,
        _button: discord.ui.Button,
        interaction: discord.Interaction,
    ) -> None:
        await interaction.response.send_modal(DraftConfigurationModal(self.cog, self.requester_id))

    @discord.ui.button(label="Publicar", emoji="✅", style=discord.ButtonStyle.success, row=0)
    async def publish(
        self,
        _button: discord.ui.Button,
        interaction: discord.Interaction,
    ) -> None:
        await interaction.response.send_modal(PublishConfigurationModal(self.cog, self.requester_id))

    @discord.ui.button(label="Accesos", emoji="🔐", style=discord.ButtonStyle.primary, row=1)
    async def access(
        self,
        _button: discord.ui.Button,
        interaction: discord.Interaction,
    ) -> None:
        await interaction.response.send_modal(AccessPolicyModal(self.cog, self.requester_id))

    @discord.ui.button(label="Agente", emoji="🤖", style=discord.ButtonStyle.primary, row=1)
    async def agent(
        self,
        _button: discord.ui.Button,
        interaction: discord.Interaction,
    ) -> None:
        if interaction.guild is None:
            return
        try:
            draft_id = await self.cog.repository.draft_revision_id(interaction.guild.id)
            if draft_id is None:
                raise LookupError("Crea un borrador antes de editar el agente.")
            current = (await self.cog.repository.snapshot_for_revision(interaction.guild.id, draft_id)).agent
            modal = AgentConfigurationModal(self.cog, self.requester_id, current)
        except Exception as error:
            await self.cog._interaction_failure(interaction, error, operation="agent.open")
            return
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Módulos", emoji="🧩", style=discord.ButtonStyle.primary, row=1)
    async def feature(
        self,
        _button: discord.ui.Button,
        interaction: discord.Interaction,
    ) -> None:
        await interaction.response.send_modal(FeatureConfigurationModal(self.cog, self.requester_id))

    @discord.ui.button(label="Personas", emoji="🎭", style=discord.ButtonStyle.primary, row=2)
    async def persona(
        self,
        _button: discord.ui.Button,
        interaction: discord.Interaction,
    ) -> None:
        await interaction.response.send_modal(
            PersonaConfigurationModal(
                self.cog,
                self.requester_id,
                include_global_profile=interaction.user.id in self.cog.owner_ids,
            )
        )

    @discord.ui.button(label="Rollback", emoji="↩️", style=discord.ButtonStyle.secondary, row=2)
    async def rollback(
        self,
        _button: discord.ui.Button,
        interaction: discord.Interaction,
    ) -> None:
        await interaction.response.send_modal(RollbackConfigurationModal(self.cog, self.requester_id))

    @discord.ui.button(
        label="Publicar consumo",
        emoji="💰",
        style=discord.ButtonStyle.secondary,
        custom_id="config:v2:cost-monitoring",
        row=2,
    )
    async def cost_monitoring(
        self,
        _button: discord.ui.Button,
        interaction: discord.Interaction,
    ) -> None:
        await self.cog.publish_cost_monitoring(interaction, self.requester_id)


class BaseConfigurationModal(discord.ui.DesignerModal):
    """Shared authorization and mutation plumbing for private forms."""

    def __init__(
        self,
        cog: ConfigurationAdminCog,
        requester_id: int,
        *children: discord.ui.ModalItem,
        title: str,
        custom_id: str,
    ) -> None:
        self.cog = cog
        self.requester_id = requester_id
        super().__init__(
            *children,
            title=title,
            custom_id=custom_id,
            timeout=CONFIG_PANEL_TIMEOUT_SECONDS,
        )

    async def run(
        self,
        interaction: discord.Interaction,
        operation: str,
        mutation: Mutation,
        *,
        owner_only: bool = False,
    ) -> None:
        await self.cog.run_mutation(
            interaction,
            self.requester_id,
            operation,
            mutation,
            owner_only=owner_only,
        )


class DraftConfigurationModal(BaseConfigurationModal):
    def __init__(self, cog: ConfigurationAdminCog, requester_id: int) -> None:
        self.action = discord.ui.StringSelect(
            custom_id="config:v2:draft:action",
            placeholder="Selecciona una acción",
            options=[
                discord.SelectOption(label="Crear borrador", value="create", emoji="📝"),
                discord.SelectOption(label="Descartar borrador", value="discard", emoji="🗑️"),
            ],
        )
        super().__init__(
            cog,
            requester_id,
            discord.ui.Label("Acción", self.action),
            title="Administrar borrador",
            custom_id=f"config:v2:draft:{requester_id}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        action = self.action.values[0]

        async def mutation() -> MutationResult:
            if interaction.guild is None:
                raise ValueError("Este panel solo funciona dentro de un servidor.")
            if action == "create":
                revision_id = await self.cog.repository.create_draft(
                    interaction.guild.id,
                    actor_user_id=interaction.user.id,
                    interaction_id=interaction.id,
                )
                return "📝 Borrador creado", f"ID: `{revision_id}`", discord.Color.blurple()
            await self.cog.repository.discard_draft(
                interaction.guild.id,
                actor_user_id=interaction.user.id,
                interaction_id=interaction.id,
            )
            return (
                "🗑️ Borrador descartado",
                "La configuración activa no cambió.",
                discord.Color.orange(),
            )

        await self.run(interaction, f"draft.{action}", mutation)


class PublishConfigurationModal(BaseConfigurationModal):
    def __init__(self, cog: ConfigurationAdminCog, requester_id: int) -> None:
        self.confirmation = discord.ui.StringSelect(
            custom_id="config:v2:publish:confirmation",
            placeholder="Confirma la publicación",
            options=[
                discord.SelectOption(
                    label="Publicar el borrador",
                    value="publish",
                    description="Reemplazará la configuración activa.",
                    emoji="✅",
                )
            ],
        )
        super().__init__(
            cog,
            requester_id,
            discord.ui.Label("Confirmación", self.confirmation),
            title="Publicar configuración",
            custom_id=f"config:v2:publish:{requester_id}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        async def mutation() -> MutationResult:
            if interaction.guild is None:
                raise ValueError("Este panel solo funciona dentro de un servidor.")
            revision_id = await self.cog.repository.publish_draft(
                interaction.guild.id,
                actor_user_id=interaction.user.id,
                interaction_id=interaction.id,
            )
            return (
                "✅ Configuración publicada",
                f"Revisión activa: `{revision_id}`.",
                discord.Color.green(),
            )

        await self.run(interaction, "config.publish", mutation)


class AccessPolicyModal(BaseConfigurationModal):
    def __init__(self, cog: ConfigurationAdminCog, requester_id: int) -> None:
        self.policy = discord.ui.StringSelect(
            custom_id="config:v2:access:policy",
            placeholder="Selecciona una política",
            options=[
                discord.SelectOption(
                    label=POLICY_LABELS[key][:100],
                    value=key.value,
                )
                for key in AccessPolicyKey
            ],
        )
        self.action = discord.ui.StringSelect(
            custom_id="config:v2:access:action",
            placeholder="Selecciona el cambio",
            options=[
                discord.SelectOption(label="Permitir todos", value="open"),
                discord.SelectOption(label="Restringir a los IDs indicados", value="restricted"),
                discord.SelectOption(label="Deshabilitar", value="disabled"),
                discord.SelectOption(label="Agregar IDs", value="add"),
                discord.SelectOption(label="Quitar IDs", value="remove"),
            ],
        )
        self.ids = discord.ui.InputText(
            custom_id="config:v2:access:ids",
            style=discord.InputTextStyle.long,
            placeholder="IDs separados por coma; acepta menciones de Discord.",
            required=False,
            max_length=2000,
        )
        super().__init__(
            cog,
            requester_id,
            discord.ui.Label("Política", self.policy),
            discord.ui.Label("Acción", self.action),
            discord.ui.Label("IDs", self.ids, description="Requeridos para restringir, agregar o quitar."),
            title="Configurar accesos",
            custom_id=f"config:v2:access:{requester_id}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        key = AccessPolicyKey(self.policy.values[0])
        action = self.action.values[0]

        async def mutation() -> MutationResult:
            if interaction.guild is None:
                raise ValueError("Este panel solo funciona dentro de un servidor.")
            current = await self.cog.draft_policy(interaction.guild.id, key)
            raw_ids = self.ids.value.strip()
            if key is AccessPolicyKey.AGENT_BLOCKED_USERS and action == "open":
                raise ValueError(
                    "Los usuarios bloqueados requieren IDs explícitos o una política deshabilitada."
                )
            if action in {"restricted", "add", "remove"} and not raw_ids:
                raise ValueError("Indica al menos un ID para esta acción.")
            if action == "open":
                mode, ids = AccessPolicyMode.OPEN, ()
            elif action == "disabled":
                mode, ids = AccessPolicyMode.DISABLED, ()
            elif action == "restricted":
                mode, ids = AccessPolicyMode.RESTRICTED, _parse_snowflakes(raw_ids)
            else:
                values = set(current.ids)
                supplied = set(_parse_snowflakes(raw_ids))
                values.difference_update(supplied) if action == "remove" else values.update(supplied)
                mode = AccessPolicyMode.RESTRICTED if values else AccessPolicyMode.DISABLED
                ids = tuple(sorted(values))
            updated = AccessPolicySnapshot(
                key=key,
                subject_type=POLICY_SUBJECT_TYPES[key],
                mode=mode,
                ids=ids,
            )
            await self.cog.repository.update_draft_policy(
                interaction.guild.id,
                updated,
                actor_user_id=interaction.user.id,
                interaction_id=interaction.id,
            )
            return (
                "🔐 Acceso actualizado",
                f"`{key.value}` → **{mode.value}** ({len(ids)} IDs).",
                discord.Color.blurple(),
            )

        await self.run(interaction, f"access.{action}", mutation)


class AgentConfigurationModal(BaseConfigurationModal):
    def __init__(
        self,
        cog: ConfigurationAdminCog,
        requester_id: int,
        current: GuildAgentConfig,
    ) -> None:
        self.model = discord.ui.StringSelect(
            custom_id="config:v2:agent:model",
            options=[
                discord.SelectOption(
                    label=model.id,
                    value=model.id,
                    default=model.id == current.model,
                )
                for model in cog.provider.model_catalog.payload.models
            ],
        )
        efforts = tuple(
            dict.fromkeys(
                effort
                for model in cog.provider.model_catalog.payload.models
                for effort in model.supported_reasoning_efforts
            )
        )
        self.reasoning = discord.ui.StringSelect(
            custom_id="config:v2:agent:reasoning",
            options=[
                discord.SelectOption(
                    label=effort,
                    value=effort,
                    default=effort == current.reasoning_effort,
                )
                for effort in efforts
            ],
        )
        self.thread_context = discord.ui.InputText(
            custom_id="config:v2:agent:thread-context",
            value=str(current.thread_context_max_messages),
            min_length=1,
            max_length=3,
        )
        self.channel_context = discord.ui.InputText(
            custom_id="config:v2:agent:channel-context",
            value=str(current.channel_context_max_messages),
            min_length=1,
            max_length=2,
        )
        self.max_turns = discord.ui.InputText(
            custom_id="config:v2:agent:max-turns",
            value=str(current.max_agent_turns),
            min_length=1,
            max_length=2,
        )
        super().__init__(
            cog,
            requester_id,
            discord.ui.Label("Modelo", self.model),
            discord.ui.Label("Reasoning", self.reasoning),
            discord.ui.Label("Mensajes de contexto en hilos", self.thread_context),
            discord.ui.Label("Mensajes de contexto en canales", self.channel_context),
            discord.ui.Label("Máximo de turnos", self.max_turns),
            title="Configurar agente",
            custom_id=f"config:v2:agent:{requester_id}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        async def mutation() -> MutationResult:
            if interaction.guild is None:
                raise ValueError("Este panel solo funciona dentro de un servidor.")
            draft_id = await self.cog.repository.draft_revision_id(interaction.guild.id)
            if draft_id is None:
                raise LookupError("Crea un borrador antes de editar el agente.")
            current = (await self.cog.repository.snapshot_for_revision(interaction.guild.id, draft_id)).agent
            model = self.model.values[0]
            reasoning = self.reasoning.values[0]
            self.cog.provider.model_catalog.resolve(model, reasoning)
            payload = current.model_dump()
            payload.update(
                {
                    "model": model,
                    "reasoning_effort": reasoning,
                    "thread_context_max_messages": int(self.thread_context.value),
                    "channel_context_max_messages": int(self.channel_context.value),
                    "max_agent_turns": int(self.max_turns.value),
                }
            )
            agent = GuildAgentConfig.model_validate(payload)
            await self.cog.repository.update_draft_agent(
                interaction.guild.id,
                agent,
                actor_user_id=interaction.user.id,
                interaction_id=interaction.id,
            )
            return (
                "🤖 Agente actualizado",
                "Los cambios quedaron guardados en el borrador.",
                discord.Color.blurple(),
            )

        await self.run(interaction, "agent.update", mutation)


class FeatureConfigurationModal(BaseConfigurationModal):
    def __init__(self, cog: ConfigurationAdminCog, requester_id: int) -> None:
        self.key = discord.ui.StringSelect(
            custom_id="config:v2:feature:key",
            placeholder="Selecciona un módulo",
            options=[
                discord.SelectOption(label="Solicitudes de roles", value="role_requests"),
                discord.SelectOption(label="Pagers", value="pagers"),
                discord.SelectOption(label="TeamSpeak", value="teamspeak"),
                discord.SelectOption(
                    label="Notificaciones de despliegue",
                    value="deployment_notifications",
                ),
                discord.SelectOption(
                    label="Acceso del propietario del reporte",
                    value="report_owner_access",
                ),
                discord.SelectOption(
                    label="Llamadas de servicio",
                    value="service_calls",
                ),
                discord.SelectOption(
                    label="Estado de facción",
                    value="faction_status",
                ),
                discord.SelectOption(
                    label="Consulta de reportes de arresto",
                    value="arrest_report_lookup",
                ),
                discord.SelectOption(
                    label="Reportes de arresto pendientes",
                    value="pending_arrest_reports",
                ),
            ],
        )
        self.enabled = discord.ui.StringSelect(
            custom_id="config:v2:feature:enabled",
            options=[
                discord.SelectOption(label="Habilitado", value="true"),
                discord.SelectOption(label="Deshabilitado", value="false"),
            ],
        )
        self.options = discord.ui.InputText(
            custom_id="config:v2:feature:options",
            style=discord.InputTextStyle.long,
            value="{}",
            max_length=4000,
        )
        super().__init__(
            cog,
            requester_id,
            discord.ui.Label("Clave del módulo", self.key),
            discord.ui.Label("Estado", self.enabled),
            discord.ui.Label("Opciones JSON", self.options),
            title="Configurar módulo",
            custom_id=f"config:v2:feature:{requester_id}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        async def mutation() -> MutationResult:
            if interaction.guild is None:
                raise ValueError("Este panel solo funciona dentro de un servidor.")
            payload = json.loads(self.options.value)
            if not isinstance(payload, dict):
                raise ValueError("Las opciones deben ser un objeto JSON.")
            key = self.key.values[0]
            draft_id = await self.cog.repository.draft_revision_id(interaction.guild.id)
            if draft_id is None:
                raise LookupError("Crea un borrador antes de configurar módulos.")
            current_snapshot = await self.cog.repository.snapshot_for_revision(
                interaction.guild.id,
                draft_id,
            )
            current = current_snapshot.features.get(key)
            payload = {**(current.payload if current is not None else {}), **payload}
            if key == "pagers" and "starting_number" not in payload:
                payload["starting_number"] = 1
            if key == "deployment_notifications" and payload:
                raise ValueError("Este módulo no admite opciones JSON.")
            if key == "service_calls" and payload:
                raise ValueError("Este módulo no admite opciones JSON.")
            if (
                key == "faction_status"
                and self.enabled.values[0] == "true"
                and "faction_id" not in payload
            ):
                raise ValueError("Estado de facción requiere `faction_id`.")
            if (
                key == "arrest_report_lookup"
                and self.enabled.values[0] == "true"
                and "faction_id" not in payload
            ):
                raise ValueError("Consulta de arrestos requiere `faction_id`.")
            if key == "pending_arrest_reports":
                payload.setdefault("max_range_days", 90)
                payload.setdefault("max_records", 1000)
                if self.enabled.values[0] == "true" and "faction_id" not in payload:
                    raise ValueError("Reportes pendientes requiere `faction_id`.")
            if key == "report_owner_access" and "max_rounds" not in payload:
                payload["max_rounds"] = 5
            feature = GuildFeatureSnapshot(
                key=key,
                enabled=self.enabled.values[0] == "true",
                schema_version=1,
                payload=payload,
            )
            await self.cog.repository.update_draft_feature(
                interaction.guild.id,
                feature,
                actor_user_id=interaction.user.id,
                interaction_id=interaction.id,
            )
            return (
                "🧩 Módulo actualizado",
                f"`{feature.key}` quedó guardado en el borrador.",
                discord.Color.blurple(),
            )

        await self.run(interaction, "feature.update", mutation)


class PersonaConfigurationModal(BaseConfigurationModal):
    def __init__(
        self,
        cog: ConfigurationAdminCog,
        requester_id: int,
        *,
        include_global_profile: bool,
    ) -> None:
        options = [
            discord.SelectOption(label="Importar borrador", value="import", emoji="📥"),
            discord.SelectOption(label="Publicar revisión", value="publish", emoji="✅"),
            discord.SelectOption(label="Asignar a la guild", value="assign", emoji="🎭"),
            discord.SelectOption(label="Revertir revisión", value="revert", emoji="↩️"),
        ]
        if include_global_profile:
            options.append(discord.SelectOption(label="Cambiar perfil global", value="global", emoji="🌐"))
        self.action = discord.ui.StringSelect(
            custom_id="config:v2:persona:action",
            placeholder="Selecciona una acción",
            options=options,
        )
        self.slug = discord.ui.InputText(
            custom_id="config:v2:persona:slug",
            placeholder="Ejemplo: roy_velazco",
            required=False,
            max_length=100,
        )
        self.revision = discord.ui.InputText(
            custom_id="config:v2:persona:revision",
            placeholder="Opcional para asignar o perfil global",
            required=False,
            max_length=10,
        )
        self.files = discord.ui.FileUpload(
            custom_id="config:v2:persona:files",
            min_values=0,
            max_values=2,
            required=False,
        )
        super().__init__(
            cog,
            requester_id,
            discord.ui.Label("Acción", self.action),
            discord.ui.Label("Identificador de persona", self.slug),
            discord.ui.Label("Revisión", self.revision),
            discord.ui.Label(
                "Archivos para importar",
                self.files,
                description="Adjunta el JSON de la persona y su avatar.",
            ),
            title="Administrar personas",
            custom_id=f"config:v2:persona:{requester_id}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        action = self.action.values[0]
        owner_only = action == "global"

        async def mutation() -> MutationResult:
            if action == "import":
                return await self._import(interaction)
            slug = self.slug.value.strip()
            if not slug:
                raise ValueError("Indica el identificador de la persona.")
            revision = int(self.revision.value) if self.revision.value.strip() else None
            if action in {"publish", "revert"} and revision is None:
                raise ValueError("Esta acción requiere una revisión.")
            if action == "publish":
                await self.cog.repository.publish_persona_revision(
                    slug,
                    revision,
                    actor_user_id=interaction.user.id,
                    interaction_id=interaction.id,
                )
                return "🎭 Persona publicada", f"`{slug}` r{revision} está disponible.", discord.Color.green()
            if action in {"assign", "revert"}:
                if interaction.guild is None:
                    raise ValueError("Este panel solo funciona dentro de un servidor.")
                await self.cog.repository.assign_draft_persona(
                    interaction.guild.id,
                    slug,
                    revision,
                    actor_user_id=interaction.user.id,
                    interaction_id=interaction.id,
                )
                title = "🎭 Persona asignada" if action == "assign" else "↩️ Persona revertida"
                return title, "La selección quedó guardada en el borrador.", discord.Color.blurple()
            await self.cog.repository.set_global_persona_by_slug(
                slug,
                revision,
                actor_user_id=interaction.user.id,
                interaction_id=interaction.id,
            )
            return (
                "🌐 Perfil global programado",
                f"`{slug}` se sincronizará de forma segura.",
                discord.Color.blurple(),
            )

        await self.run(interaction, f"persona.{action}", mutation, owner_only=owner_only)

    async def _import(self, interaction: discord.Interaction) -> MutationResult:
        attachments = tuple(self.files.values or ())
        profile_attachment = next(
            (item for item in attachments if item.filename.lower().endswith(".json")),
            None,
        )
        avatar_attachment = next(
            (
                item
                for item in attachments
                if item is not profile_attachment
                and (
                    (item.content_type or "").lower() in {"image/png", "image/jpeg", "image/gif"}
                    or item.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif"))
                )
            ),
            None,
        )
        if profile_attachment is None or avatar_attachment is None:
            raise ValueError("Adjunta un perfil JSON y un avatar PNG, JPEG o GIF.")
        profile_bytes = await profile_attachment.read()
        avatar_bytes = await avatar_attachment.read()
        if len(profile_bytes) > MAX_PERSONA_JSON_BYTES:
            raise ValueError("El JSON de persona es demasiado grande.")
        if not avatar_bytes or len(avatar_bytes) > MAX_PERSONA_AVATAR_BYTES:
            raise ValueError("El avatar debe pesar entre 1 byte y 10 MiB.")
        profile = BotPersona.model_validate_json(profile_bytes)
        content_type = avatar_attachment.content_type or mimetypes.guess_type(avatar_attachment.filename)[0]
        if content_type not in {"image/png", "image/jpeg", "image/gif"}:
            raise ValueError("El avatar debe ser PNG, JPEG o GIF.")
        record = await self.cog.repository.import_persona_draft(
            profile,
            avatar_bytes,
            content_type=content_type,
            actor_user_id=interaction.user.id,
            interaction_id=interaction.id,
        )
        return (
            "🎭 Persona importada",
            f"`{record.slug}` r{record.revision} quedó como borrador.",
            discord.Color.blurple(),
        )


class RollbackConfigurationModal(BaseConfigurationModal):
    def __init__(self, cog: ConfigurationAdminCog, requester_id: int) -> None:
        self.revision = discord.ui.InputText(
            custom_id="config:v2:rollback:revision",
            placeholder="Número de revisión histórica",
            min_length=1,
            max_length=10,
        )
        super().__init__(
            cog,
            requester_id,
            discord.ui.Label("Revisión", self.revision),
            title="Restaurar configuración",
            custom_id=f"config:v2:rollback:{requester_id}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        async def mutation() -> MutationResult:
            if interaction.guild is None:
                raise ValueError("Este panel solo funciona dentro de un servidor.")
            revision_id = await self.cog.repository.rollback(
                interaction.guild.id,
                int(self.revision.value),
                actor_user_id=interaction.user.id,
                interaction_id=interaction.id,
            )
            return (
                "↩️ Configuración restaurada",
                f"Nueva revisión activa: `{revision_id}`.",
                discord.Color.orange(),
            )

        await self.run(interaction, "config.rollback", mutation)


def _parse_snowflakes(raw_value: str) -> tuple[int, ...]:
    values: set[int] = set()
    for item in raw_value.split(","):
        normalized = item.strip().removeprefix("<@&").removeprefix("<@!").removeprefix("<@")
        normalized = normalized.removeprefix("<#").removesuffix(">")
        if not normalized:
            continue
        value = int(normalized)
        if value <= 0:
            raise ValueError("Los IDs deben ser snowflakes positivos de Discord.")
        values.add(value)
    if not values:
        raise ValueError("Proporciona al menos un ID de Discord.")
    return tuple(sorted(values))
