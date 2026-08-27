"""Discord command for the live Field Training Program summary."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import discord
from discord.ext import commands

from arrest_report_agent.config import BotPersona
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider

from .models import FtpMemberGroup, FtpPresenceMember, FtpPresenceSnapshot
from .service import FtpStatusService

LOGGER = logging.getLogger(__name__)
MAX_DISPLAYED_MEMBERS_PER_GROUP = 10
GROUP_PRESENTATION: tuple[tuple[FtpMemberGroup, str], ...] = (
    (FtpMemberGroup.SUPERVISORY, "🧑‍💼 Supervisory Staff"),
    (FtpMemberGroup.MFTO, "🧑‍🏫 Master FTO (MFTO)"),
    (FtpMemberGroup.FTO, "👨‍🏫 Field Training Officers (FTO)"),
    (FtpMemberGroup.FTT, "🧑‍🎓 Field Training Trainees (FTT)"),
)


class FtpStatusCog(commands.Cog):
    """Show only currently connected members associated with the FTP."""

    def __init__(
        self,
        provider: DatabaseConfigProvider,
        service: FtpStatusService,
        persona: BotPersona,
    ) -> None:
        self.provider = provider
        self.service = service
        self.default_persona = persona

    @discord.slash_command(
        name="ftp-conectados",
        description="Muestra los miembros conectados del Field Training Program",
    )
    async def ftp_connected(self, ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            await ctx.respond(
                embed=_alert(
                    "⚠️ FTP no disponible",
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
                await ctx.respond(
                    embed=_alert("⚠️ FTP no disponible", ui.unauthorized),
                    ephemeral=True,
                )
                return
            options = bundle.settings.ftp_status_options
            if options is None:
                await ctx.respond(
                    embed=_alert(
                        "⚠️ FTP no disponible",
                        "El resumen de miembros conectados del FTP no está habilitado para este servidor.",
                    ),
                    ephemeral=True,
                )
                return

            await ctx.defer(ephemeral=False)
            deferred = True
            snapshot = await self.service.query(guild_id, options)
            await ctx.followup.send(
                embed=build_ftp_status_embed(snapshot),
                ephemeral=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log_event(
                LOGGER,
                "info",
                "ftp_status_published",
                guild_id=guild_id,
                user_id=user_id,
                faction_id=options.faction_id,
                connected_members=len(snapshot.members),
                on_duty_members=snapshot.on_duty_count,
                ambiguous_connected_characters=snapshot.ambiguous_connected_characters,
            )
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "ftp_status_command_failed",
                guild_id=guild_id,
                user_id=user_id,
                exc_info=error,
                **exception_fields(error),
            )
            embed = _alert("⚠️ FTP no disponible", ui.operation_failed)
            if deferred:
                await ctx.followup.send(embed=embed, ephemeral=True)
            else:
                await ctx.respond(embed=embed, ephemeral=True)


def build_ftp_status_embed(snapshot: FtpPresenceSnapshot) -> discord.Embed:
    """Render separated, bounded FTP groups containing connected members only."""

    counts = {group: len(snapshot.members_for(group)) for group, _ in GROUP_PRESENTATION}
    summary = (
        f"**Supervisores:** {counts[FtpMemberGroup.SUPERVISORY]} · "
        f"**MFTO:** {counts[FtpMemberGroup.MFTO]} · "
        f"**FTO:** {counts[FtpMemberGroup.FTO]} · "
        f"**FTT:** {counts[FtpMemberGroup.FTT]} · "
        f"**En servicio:** {snapshot.on_duty_count}"
    )
    introduction = (
        "Personal conectado actualmente relacionado con el Field Training Program."
        if snapshot.members
        else "No aparece ningún integrante del Field Training Program conectado ahora mismo."
    )
    embed = discord.Embed(
        title="🎓 FTP · Miembros conectados",
        description=f"{introduction}\n\n{summary}",
        color=discord.Color.gold(),
        timestamp=datetime.now(UTC),
    )
    for group, title in GROUP_PRESENTATION:
        members = snapshot.members_for(group)
        if members:
            embed.add_field(
                name=f"{title} · {len(members)}",
                value=_render_member_group(members),
                inline=False,
            )
    embed.set_footer(text="Solo se muestran miembros conectados en este momento.")
    return embed


def _render_member_group(members: tuple[FtpPresenceMember, ...]) -> str:
    shown = members[:MAX_DISPLAYED_MEMBERS_PER_GROUP]
    lines = [
        f"`{'En servicio' if member.on_duty else 'Fuera de servicio'}` <@{member.user_id}>"
        for member in shown
    ]
    remaining = len(members) - len(shown)
    if remaining > 0:
        lines.append(f"… y {remaining} más.")
    return "\n".join(lines)


def _member_has_any_role(member: discord.Member, role_ids: tuple[int, ...]) -> bool:
    allowed = set(role_ids)
    return bool(allowed and any(role.id in allowed for role in member.roles))


def _alert(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=discord.Color.red())
