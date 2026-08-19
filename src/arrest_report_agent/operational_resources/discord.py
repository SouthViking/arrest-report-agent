"""Public Discord panel for links and live operational status."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import discord
from discord.ext import commands

from arrest_report_agent.config import (
    BotPersona,
    OperationalResourcesOptions,
    PersonaOperationalResourcesMessages,
)
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider

from .models import OperationalResourcesSnapshot
from .service import OperationalResourcesService

LOGGER = logging.getLogger(__name__)


class OperationalResourcesCog(commands.Cog):
    """Expose operational links and live status to configured staff roles."""

    def __init__(
        self,
        provider: DatabaseConfigProvider,
        service: OperationalResourcesService,
        persona: BotPersona,
    ) -> None:
        self.provider = provider
        self.service = service
        self.default_persona = persona
        self.resources.description = persona.messages.interface.commands.resources_description

    @discord.slash_command(
        name="recursos",
        description="Muestra accesos y estado operativo de la facción",
    )
    async def resources(self, ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            await ctx.respond(
                embed=_alert(
                    "⚠️ Recursos no disponibles",
                    "Este comando solo funciona dentro de un servidor.",
                ),
                ephemeral=True,
            )
            return
        guild_id = ctx.guild.id
        user_id = ctx.author.id
        deferred = False
        ui = self.default_persona.messages.interface.operational_resources
        try:
            bundle = await self.provider.cached(guild_id)
            ui = bundle.persona.profile.messages.interface.operational_resources
            if not _member_has_any_role(
                ctx.author,
                bundle.settings.operational_staff_role_ids,
            ):
                await ctx.respond(embed=_alert(ui.alert_title, ui.unauthorized), ephemeral=True)
                return
            options = bundle.settings.operational_resources_options
            if options is None:
                await ctx.respond(embed=_alert(ui.alert_title, ui.unavailable), ephemeral=True)
                return
            await ctx.defer(ephemeral=False)
            deferred = True
            snapshot = await self.service.query(options.faction_id)
            thumbnail_url = str(ctx.guild.icon.url) if ctx.guild.icon is not None else None
            await ctx.followup.send(
                embed=build_operational_resources_embed(
                    snapshot,
                    options,
                    teamspeak_address=bundle.settings.teamspeak_address,
                    organization_acronym=bundle.persona.profile.identity.organization_acronym,
                    ui=ui,
                    thumbnail_url=thumbnail_url,
                ),
                ephemeral=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            status = snapshot.faction_status
            log_event(
                LOGGER,
                "info",
                "operational_resources_published",
                guild_id=guild_id,
                user_id=user_id,
                faction_id=options.faction_id,
                connected_members=status.connected_count,
                on_duty_members=status.on_duty_count,
                total_members=status.total_members,
                server_connected_accounts=status.server_connected_count,
                server_cache_available=snapshot.server_cache is not None,
            )
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "operational_resources_command_failed",
                guild_id=guild_id,
                user_id=user_id,
                exc_info=error,
                **exception_fields(error),
            )
            embed = _alert(ui.alert_title, ui.operation_failed)
            if deferred:
                await ctx.followup.send(embed=embed, ephemeral=True)
            else:
                await ctx.respond(embed=embed, ephemeral=True)


def build_operational_resources_embed(
    snapshot: OperationalResourcesSnapshot,
    options: OperationalResourcesOptions,
    *,
    teamspeak_address: str,
    organization_acronym: str,
    ui: PersonaOperationalResourcesMessages,
    thumbnail_url: str | None = None,
) -> discord.Embed:
    """Render one bounded panel without exposing internal identifiers."""

    status = snapshot.faction_status
    server_count = (
        str(status.server_connected_count)
        if status.server_connected_count is not None
        else "No disponible"
    )
    embed = discord.Embed(
        title="📡 Recursos y estado operativo",
        description=ui.intro,
        color=discord.Color.blurple(),
        timestamp=datetime.now(UTC),
    )
    embed.add_field(
        name="📊 Estado actual",
        value=(
            f"└─ 🌐 **Servidor:** {server_count} · "
            f"🟢 **Facción:** {status.connected_count} · "
            f"🚓 **Servicio:** {status.on_duty_count} · "
            f"📈 **Actividad:** {status.duty_percentage}%"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎮 Conexiones",
        value=(
            f"└─ **Servidor:** `{_safe_address(options.game_server_address)}`\n"
            f"└─ **TeamSpeak 3:** `{_safe_address(teamspeak_address)}`"
        ),
        inline=False,
    )
    embed.add_field(
        name="🔗 Accesos rápidos",
        value=(
            f"└─ [PCU]({options.pcu_url})\n"
            f"└─ [Foro {_safe_label(organization_acronym)}]({options.forum_url})\n"
            f"└─ [Manual {_safe_label(organization_acronym)}]({options.manual_url})\n"
            f"└─ [Código penal]({options.penal_code_url})"
        ),
        inline=False,
    )
    cache = snapshot.server_cache
    if cache is not None and cache.url is not None and cache.version is not None:
        embed.add_field(
            name="📦 Recursos del servidor",
            value=(
                f"└─ [Descargar caché oficial]({cache.url})\n"
                f"└─ **Versión:** {cache.version} · "
                f"**Tamaño:** {_format_bytes(cache.size_bytes or 0)}"
            ),
            inline=False,
        )
    embed.add_field(
        name="👥 Detalle del personal",
        value=f"└─ {ui.connected_reference}",
        inline=False,
    )
    if thumbnail_url is not None:
        embed.set_thumbnail(url=thumbnail_url)
    return embed


def _format_bytes(size: int) -> str:
    value = float(size)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}"
        value /= 1024
    raise AssertionError("unreachable byte formatter")


def _safe_address(value: str) -> str:
    return value.replace("`", "").strip()


def _safe_label(value: str) -> str:
    return discord.utils.escape_markdown(value.replace("[", "").replace("]", "").strip())


def _member_has_any_role(member: discord.Member, role_ids: tuple[int, ...]) -> bool:
    allowed = set(role_ids)
    return bool(allowed and any(role.id in allowed for role in member.roles))


def _alert(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=discord.Color.red())
