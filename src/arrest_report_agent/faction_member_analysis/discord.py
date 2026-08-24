"""Restricted Discord command for deterministic faction-member analysis."""

from __future__ import annotations

import logging
from enum import StrEnum

import discord
from discord.ext import commands

from arrest_report_agent.config import BotPersona, PersonaFactionMemberAnalysisMessages
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.faction_directory import FactionActivityLevel
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider

from .models import FactionMemberAnalysisSnapshot
from .service import FactionMemberAnalysisService

LOGGER = logging.getLogger(__name__)
MODAL_TIMEOUT_SECONDS = 300


class FactionMemberAnalysisVisibility(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"


class FactionMemberAnalysisCog(commands.Cog):
    """Expose a compact faction analysis to explicitly authorized leadership roles."""

    def __init__(
        self,
        provider: DatabaseConfigProvider,
        service: FactionMemberAnalysisService,
        persona: BotPersona,
    ) -> None:
        self.provider = provider
        self.service = service
        self.default_persona = persona
        self.member_activity.description = (
            persona.messages.interface.commands.faction_member_analysis_description
        )

    @discord.slash_command(
        name="actividad-miembros",
        description="Resume la actividad general de los miembros de la facción",
    )
    async def member_activity(self, ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            await ctx.respond(
                embed=_alert(
                    "⚠️ Análisis no disponible",
                    "Este comando solo funciona dentro de un servidor.",
                ),
                ephemeral=True,
            )
            return
        try:
            bundle = await self.provider.cached(ctx.guild.id)
            ui = bundle.persona.profile.messages.interface.faction_member_analysis
            if not _member_has_any_role(
                ctx.author,
                bundle.settings.supervisor_role_ids,
            ):
                await ctx.respond(embed=_alert(ui.alert_title, ui.unauthorized), ephemeral=True)
                return
            if bundle.settings.faction_member_analysis_options is None:
                await ctx.respond(embed=_alert(ui.alert_title, ui.unavailable), ephemeral=True)
                return
            await ctx.send_modal(FactionMemberAnalysisModal(self, requester_id=ctx.author.id))
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "faction_member_analysis_command_failed",
                guild_id=ctx.guild.id,
                user_id=ctx.author.id,
                exc_info=error,
                **exception_fields(error),
            )
            ui = self.default_persona.messages.interface.faction_member_analysis
            await ctx.respond(embed=_alert(ui.alert_title, ui.operation_failed), ephemeral=True)

    async def publish(
        self,
        interaction: discord.Interaction,
        *,
        requester_id: int,
        visibility: FactionMemberAnalysisVisibility,
    ) -> None:
        if (
            interaction.guild is None
            or interaction.channel is None
            or not isinstance(interaction.user, discord.Member)
            or interaction.user.id != requester_id
        ):
            await interaction.response.send_message(
                embed=_alert("⚠️ Análisis no disponible", "Esta consulta ya no es válida."),
                ephemeral=True,
            )
            return
        bundle = await self.provider.cached(interaction.guild.id)
        ui = bundle.persona.profile.messages.interface.faction_member_analysis
        if not _member_has_any_role(
            interaction.user,
            bundle.settings.supervisor_role_ids,
        ):
            await interaction.response.send_message(
                embed=_alert(ui.alert_title, ui.unauthorized),
                ephemeral=True,
            )
            return
        options = bundle.settings.faction_member_analysis_options
        if options is None:
            await interaction.response.send_message(
                embed=_alert(ui.alert_title, ui.unavailable),
                ephemeral=True,
            )
            return
        private = visibility is FactionMemberAnalysisVisibility.PRIVATE
        await interaction.response.defer(ephemeral=private)
        try:
            snapshot = await self.service.analyze(options)
            await interaction.followup.send(
                embed=build_faction_member_analysis_embed(snapshot, ui),
                ephemeral=private,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log_event(
                LOGGER,
                "info",
                "faction_member_analysis_published",
                guild_id=interaction.guild.id,
                user_id=interaction.user.id,
                faction_id=options.faction_id,
                visibility=visibility.value,
                total_members=snapshot.total_members,
                connected_members=snapshot.connected_members,
                on_duty_members=snapshot.on_duty_members,
                inactive_members=snapshot.over_warning_days,
                critical_inactive_members=snapshot.over_critical_days,
                unassigned_divisions=snapshot.unassigned_divisions,
            )
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "faction_member_analysis_failed",
                guild_id=interaction.guild.id,
                user_id=interaction.user.id,
                faction_id=options.faction_id,
                visibility=visibility.value,
                exc_info=error,
                **exception_fields(error),
            )
            await interaction.followup.send(
                embed=_alert(ui.alert_title, ui.operation_failed),
                ephemeral=private,
            )


class FactionMemberAnalysisModal(discord.ui.DesignerModal):
    """Collect only the requested result visibility."""

    def __init__(self, cog: FactionMemberAnalysisCog, requester_id: int) -> None:
        self.cog = cog
        self.requester_id = requester_id
        self.publish_for_everyone = discord.ui.Checkbox(
            custom_id="faction_member_analysis:v1:publish",
            default=True,
        )
        super().__init__(
            discord.ui.Label("Publicar el análisis en este canal", self.publish_for_everyone),
            title="Analizar miembros de la facción",
            custom_id=f"faction_member_analysis:v1:run:{requester_id}",
            timeout=MODAL_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.publish(
            interaction,
            requester_id=self.requester_id,
            visibility=(
                FactionMemberAnalysisVisibility.PUBLIC
                if self.publish_for_everyone.value
                else FactionMemberAnalysisVisibility.PRIVATE
            ),
        )


def build_faction_member_analysis_embed(
    snapshot: FactionMemberAnalysisSnapshot,
    ui: PersonaFactionMemberAnalysisMessages,
) -> discord.Embed:
    color = (
        discord.Color.red()
        if snapshot.is_critical
        else discord.Color.orange()
        if snapshot.has_attention_items
        else discord.Color.green()
    )
    embed = discord.Embed(
        title="👥 Análisis de miembros",
        description=ui.intro,
        color=color,
        timestamp=snapshot.generated_at,
    )
    embed.add_field(name="👥 Miembros", value=str(snapshot.total_members), inline=True)
    embed.add_field(name="🟢 Conectados", value=str(snapshot.connected_members), inline=True)
    embed.add_field(name="🚓 En servicio", value=str(snapshot.on_duty_members), inline=True)
    activity_icons = {
        FactionActivityLevel.VERY_ACTIVE: "🟢",
        FactionActivityLevel.ACTIVE: "🟡",
        FactionActivityLevel.REGULAR: "🔵",
        FactionActivityLevel.AT_RISK: "🟠",
        FactionActivityLevel.INACTIVE: "🔴",
        FactionActivityLevel.UNKNOWN: "⚪",
    }
    activity_lines = [
        (
            f"{activity_icons[item.level]} **{item.level.value}:** {item.count} · "
            f"{_percentage(snapshot.percentage(item.count))}"
        )
        for item in snapshot.activity
    ]
    embed.add_field(
        name="📈 Actividad según última conexión",
        value="\n".join(activity_lines),
        inline=False,
    )
    distribution = [
        *((item.name, item.count) for item in snapshot.divisions if item.is_base),
        ("Especialidades", snapshot.specialty_count),
        ("Sin asignación", snapshot.unassigned_divisions),
    ]
    distribution_lines = [
        f"**{name}:** {count}"
        for name, count in sorted(
            distribution,
            key=lambda item: (-item[1], item[0].casefold()),
        )
    ]
    embed.add_field(
        name="🏢 Distribución",
        value="\n".join(distribution_lines),
        inline=False,
    )
    specialties = [
        f"**{item.name}:** {item.count}"
        for item in sorted(
            (
                item
                for item in snapshot.divisions
                if not item.is_base and item.count > 0
            ),
            key=lambda item: (-item.count, item.name.casefold()),
        )
    ]
    if specialties:
        embed.add_field(name="🧩 Especialidades", value="\n".join(specialties), inline=False)
    embed.add_field(
        name="⚠️ Atención" if snapshot.has_attention_items else "✅ Estado general",
        value="\n".join(_attention_lines(snapshot)) if snapshot.has_attention_items else ui.no_alerts,
        inline=False,
    )
    return embed


def _attention_lines(snapshot: FactionMemberAnalysisSnapshot) -> tuple[str, ...]:
    lines: list[str] = []
    if snapshot.over_warning_days:
        lines.append(
            f"• {_member_count(snapshot.over_warning_days)} "
            f"{_exceed_verb(snapshot.over_warning_days)} los "
            f"{snapshot.inactivity_warning_days} días sin conectar "
            f"({_percentage(snapshot.percentage(snapshot.over_warning_days))}; "
            f"umbral de atención: {snapshot.inactivity_warning_percentage}%)."
        )
    if snapshot.over_critical_days:
        lines.append(
            f"• {_member_count(snapshot.over_critical_days)} "
            f"{_exceed_verb(snapshot.over_critical_days)} los "
            f"{snapshot.inactivity_critical_days} días sin conectar."
        )
    if snapshot.unassigned_divisions:
        lines.append(
            f"• {_member_count(snapshot.unassigned_divisions)} "
            f"{_have_verb(snapshot.unassigned_divisions)} división asignada."
        )
    if snapshot.unknown_activity:
        lines.append(
            f"• {_member_count(snapshot.unknown_activity)} "
            f"{_have_verb(snapshot.unknown_activity)} una última conexión válida."
        )
    if snapshot.invalid_divisions:
        lines.append(
            f"• {_member_count(snapshot.invalid_divisions)} "
            f"{_has_verb(snapshot.invalid_divisions)} una división fuera del catálogo."
        )
    if snapshot.inconsistent_duty_state:
        lines.append(
            f"• {_member_count(snapshot.inconsistent_duty_state)} "
            f"{_appear_verb(snapshot.inconsistent_duty_state)} en servicio pero "
            f"{_disconnected_word(snapshot.inconsistent_duty_state)}."
        )
    return tuple(lines)


def _percentage(value: float) -> str:
    return f"{value:.1f}%".replace(".", ",")


def _member_count(count: int) -> str:
    return f"{count} miembro" if count == 1 else f"{count} miembros"


def _exceed_verb(count: int) -> str:
    return "supera" if count == 1 else "superan"


def _have_verb(count: int) -> str:
    return "no tiene" if count == 1 else "no tienen"


def _has_verb(count: int) -> str:
    return "tiene" if count == 1 else "tienen"


def _appear_verb(count: int) -> str:
    return "figura" if count == 1 else "figuran"


def _disconnected_word(count: int) -> str:
    return "desconectado" if count == 1 else "desconectados"


def _member_has_any_role(member: discord.Member, role_ids: tuple[int, ...]) -> bool:
    allowed = set(role_ids)
    return bool(allowed and any(role.id in allowed for role in member.roles))


def _alert(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=discord.Color.red())
