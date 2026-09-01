"""Deterministic public Discord command for SARP evidence bags."""

from __future__ import annotations

import logging
from datetime import datetime

import discord
from discord.ext import commands

from arrest_report_agent.application.ports import EvidenceBagDetail, EvidenceBagLookup
from arrest_report_agent.config import BotPersona
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider

LOGGER = logging.getLogger(__name__)
MAX_FIELD_LENGTH = 1024


class EvidenceBagCog(commands.Cog):
    """Expose live evidence-bag details to configured operational staff."""

    def __init__(
        self,
        provider: DatabaseConfigProvider,
        service: EvidenceBagLookup,
        persona: BotPersona,
    ) -> None:
        self.provider = provider
        self.service = service
        self.default_persona = persona
        self.view_evidence.description = "Consulta una bolsa de evidencias"

    @discord.slash_command(
        name="ver-evidencia",
        description="Consulta una bolsa de evidencias",
    )
    @discord.option(  # type: ignore[no-untyped-call,untyped-decorator]
        "bolsa",
        int,
        description="Número de la bolsa",
        min_value=1,
    )
    async def view_evidence(
        self,
        ctx: discord.ApplicationContext,
        bolsa: int,
    ) -> None:
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            await ctx.respond(
                embed=_alert("Este comando solo funciona dentro de un servidor."),
                ephemeral=True,
            )
            return

        guild_id = ctx.guild.id
        user_id = ctx.author.id
        evidence_id = int(bolsa)
        deferred = False
        try:
            bundle = await self.provider.cached(guild_id)
            if not _member_has_any_role(
                ctx.author,
                bundle.settings.operational_staff_role_ids,
            ):
                await ctx.respond(
                    embed=_alert("Esta consulta está reservada al personal operativo autorizado."),
                    ephemeral=True,
                )
                return
            if bundle.settings.arrest_report_lookup_faction_id is None:
                await ctx.respond(
                    embed=_alert("La consulta de evidencias no está habilitada para este servidor."),
                    ephemeral=True,
                )
                return

            await ctx.defer(ephemeral=False)
            deferred = True
            result = await self.service.get(evidence_id)
            if result.error is not None or result.detail is None:
                await ctx.followup.send(
                    embed=_lookup_error(evidence_id, result.error),
                    ephemeral=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            await ctx.followup.send(
                embed=build_evidence_bag_embed(
                    result.detail,
                    requested_by=ctx.author.display_name,
                ),
                ephemeral=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log_event(
                LOGGER,
                "info",
                "evidence_bag_command_published",
                guild_id=guild_id,
                channel_id=ctx.channel_id,
                user_id=user_id,
                evidence_id=evidence_id,
                item_count=len(result.detail.items),
                custody_event_count=len(result.detail.custody),
            )
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "evidence_bag_command_failed",
                guild_id=guild_id,
                user_id=user_id,
                evidence_id=evidence_id,
                exc_info=error,
                **exception_fields(error),
            )
            embed = _alert("No pude consultar esa bolsa de evidencias en este momento.")
            if deferred:
                await ctx.followup.send(
                    embed=embed,
                    ephemeral=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await ctx.respond(embed=embed, ephemeral=True)


def build_evidence_bag_embed(
    detail: EvidenceBagDetail,
    *,
    requested_by: str | None = None,
) -> discord.Embed:
    """Render one bounded embed without reinterpreting SARP result text."""

    embed = discord.Embed(
        title=f"📦 Bolsa de evidencias #{detail.evidence_id}",
        description="Contenido registrado y cadena de custodia disponible en el sistema policial.",
        color=discord.Color.dark_teal(),
    )
    item_lines = [f"• {item.result}" for item in detail.items]
    custody_lines = [
        f"• {_discord_time(entry.created_at)} · {entry.officer_name} · {entry.action}"
        for entry in detail.custody
    ]
    embed.add_field(
        name=f"🔬 Contenido registrado · {len(detail.items)}",
        value=_bounded_lines(
            item_lines,
            empty=(
                "La bolsa existe, pero todavía no presenta resultados de laboratorio. "
                "Puede no haber sido analizada."
            ),
        ),
        inline=False,
    )
    embed.add_field(
        name=f"🔗 Cadena de custodia · {len(detail.custody)}",
        value=_bounded_lines(
            custody_lines,
            empty="No hay movimientos de custodia registrados.",
        ),
        inline=False,
    )
    if requested_by:
        embed.set_footer(text=f"Consulta realizada por {requested_by}")
    return embed


def _bounded_lines(lines: list[str], *, empty: str) -> str:
    if not lines:
        return empty
    selected: list[str] = []
    used = 0
    for line in lines:
        normalized = " ".join(line.split())
        extra = len(normalized) + (1 if selected else 0)
        if used + extra > MAX_FIELD_LENGTH - 30:
            break
        selected.append(normalized)
        used += extra
    hidden = len(lines) - len(selected)
    if hidden:
        selected.append(f"… y {hidden} registro(s) más.")
    return "\n".join(selected)


def _lookup_error(evidence_id: int, error: str | None) -> discord.Embed:
    if error == "not_found":
        description = f"No encontré la bolsa de evidencias `#{evidence_id}`."
    elif error == "rate_limited":
        description = "La API alcanzó temporalmente su límite de consultas. Inténtelo nuevamente."
    else:
        description = "No pude consultar esa bolsa de evidencias en este momento."
    return _alert(description)


def _alert(description: str) -> discord.Embed:
    return discord.Embed(
        title="⚠️ Evidencia no disponible",
        description=description,
        color=discord.Color.orange(),
    )


def _discord_time(value: datetime) -> str:
    return f"<t:{int(value.timestamp())}:f>"


def _member_has_any_role(member: discord.Member, allowed_role_ids: tuple[int, ...]) -> bool:
    if not allowed_role_ids:
        return False
    return any(role.id in allowed_role_ids for role in member.roles)
