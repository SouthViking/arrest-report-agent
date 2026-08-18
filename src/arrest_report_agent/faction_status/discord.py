"""Discord command for deterministic live faction presence."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import discord
from discord.ext import commands

from arrest_report_agent.config import BotPersona, PersonaFactionStatusMessages
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider

from .models import FactionPresenceSnapshot
from .service import FactionStatusService

LOGGER = logging.getLogger(__name__)
MAX_DISPLAYED_MEMBERS_PER_GROUP = 10


class FactionStatusCog(commands.Cog):
    """Expose the configured SARP faction roster to operational staff."""

    def __init__(
        self,
        provider: DatabaseConfigProvider,
        service: FactionStatusService,
        persona: BotPersona,
    ) -> None:
        self.provider = provider
        self.service = service
        self.default_persona = persona
        self.connected_members.description = (
            persona.messages.interface.commands.connected_members_description
        )

    @discord.slash_command(
        name="conectados",
        description="Muestra el estado de conexión de la facción",
    )
    async def connected_members(
        self,
        ctx: discord.ApplicationContext,
    ) -> None:
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            await ctx.respond(
                embed=_alert(
                    "⚠️ Estado de facción no disponible",
                    "Este comando solo funciona dentro de un servidor.",
                ),
                ephemeral=True,
            )
            return
        guild_id = ctx.guild.id
        user_id = ctx.author.id
        deferred = False
        ui = self.default_persona.messages.interface.faction_status
        try:
            bundle = await self.provider.cached(guild_id)
            ui = bundle.persona.profile.messages.interface.faction_status
            if not _member_has_any_role(
                ctx.author,
                bundle.settings.operational_staff_role_ids,
            ):
                await ctx.respond(embed=_alert(ui.alert_title, ui.unauthorized), ephemeral=True)
                return
            faction_id = bundle.settings.faction_status_faction_id
            if faction_id is None:
                await ctx.respond(embed=_alert(ui.alert_title, ui.unavailable), ephemeral=True)
                return
            await ctx.defer(ephemeral=False)
            deferred = True
            snapshot = await self.service.query(faction_id)
            await ctx.followup.send(
                embed=build_faction_status_embed(snapshot, ui),
                ephemeral=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log_event(
                LOGGER,
                "info",
                "faction_status_published",
                guild_id=guild_id,
                user_id=user_id,
                faction_id=faction_id,
                connected_members=snapshot.connected_count,
                on_duty_members=snapshot.on_duty_count,
                total_members=snapshot.total_members,
            )
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "faction_status_command_failed",
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


def build_faction_status_embed(
    snapshot: FactionPresenceSnapshot,
    ui: PersonaFactionStatusMessages,
) -> discord.Embed:
    """Render a bounded, alphabetically ordered faction presence list."""

    embed = discord.Embed(
        title="👥 Estado de la facción",
        description=ui.connected_intro if snapshot.connected_members else ui.no_connected,
        color=discord.Color.green(),
        timestamp=datetime.now(UTC),
    )
    embed.add_field(
        name="Conectados",
        value=f"**{snapshot.connected_count}**",
        inline=True,
    )
    embed.add_field(
        name="En servicio",
        value=f"**{snapshot.on_duty_count}**",
        inline=True,
    )
    embed.add_field(
        name="Actividad operativa",
        value=f"**{snapshot.duty_percentage}%**",
        inline=True,
    )
    if snapshot.connected_members:
        on_duty = tuple(member.name for member in snapshot.connected_members if member.on_duty)
        off_duty = tuple(member.name for member in snapshot.connected_members if not member.on_duty)
        if on_duty:
            embed.add_field(
                name=f"🟢 En servicio · {len(on_duty)}",
                value=_render_member_group(on_duty),
                inline=False,
            )
        if off_duty:
            embed.add_field(
                name=f"▫️ Conectados, fuera de servicio · {len(off_duty)}",
                value=_render_member_group(off_duty),
                inline=False,
            )
    return embed


def _render_member_group(names: tuple[str, ...]) -> str:
    shown = names[:MAX_DISPLAYED_MEMBERS_PER_GROUP]
    lines = [discord.utils.escape_markdown(name) for name in shown]
    remaining = len(names) - len(shown)
    if remaining > 0:
        lines.append(f"… y {remaining} más.")
    return "\n".join(lines)


def _member_has_any_role(member: discord.Member, role_ids: tuple[int, ...]) -> bool:
    allowed = set(role_ids)
    return bool(allowed and any(role.id in allowed for role in member.roles))


def _alert(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=discord.Color.red())
