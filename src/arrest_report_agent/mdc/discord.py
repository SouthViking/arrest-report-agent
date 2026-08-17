"""Discord presentation for deterministic, IC-safe MDC lookups."""

from __future__ import annotations

import logging
from contextlib import suppress
from datetime import UTC, datetime

import discord
from discord.ext import commands

from arrest_report_agent.config import BotPersona, PersonaMdcMessages
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider

from .models import (
    MdcArrest,
    MdcCharacterAmbiguousError,
    MdcCharacterNotFoundError,
    MdcRecord,
    MdcSection,
    MdcWarrant,
    normalize_character_name,
)
from .service import MdcService

LOGGER = logging.getLogger(__name__)
MODAL_TIMEOUT_SECONDS = 300
MAX_LISTED_ITEMS = 3
SECTION_LABELS = {
    MdcSection.STATUS: "estado de custodia",
    MdcSection.POSSESSIONS: "posesiones registradas",
    MdcSection.FINES: "multas pendientes",
    MdcSection.WARRANTS: "órdenes activas",
    MdcSection.ARRESTS: "antecedentes de arresto",
}


class MdcCog(commands.Cog):
    """Expose the SARP police data projection to configured operational staff."""

    def __init__(
        self,
        provider: DatabaseConfigProvider,
        service: MdcService,
        persona: BotPersona,
    ) -> None:
        self.provider = provider
        self.service = service
        self.default_persona = persona
        self.mdc.description = persona.messages.interface.commands.mdc_description

    @discord.slash_command(name="mdc", description="Consulta un personaje en el MDC")
    async def mdc(self, ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            await ctx.respond(
                embed=_generic_alert("Este comando solo funciona dentro de un servidor."),
                ephemeral=True,
            )
            return
        try:
            bundle = await self.provider.cached(ctx.guild.id)
            ui = bundle.persona.profile.messages.interface.mdc
            if not _member_has_any_role(ctx.author, bundle.settings.operational_staff_role_ids):
                await ctx.respond(embed=_alert(ui.alert_title, ui.unauthorized), ephemeral=True)
                return
            await ctx.send_modal(MdcSearchModal(self, ctx.author.id))
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "mdc_command_failed",
                guild_id=ctx.guild.id,
                user_id=ctx.author.id,
                exc_info=error,
                **exception_fields(error),
            )
            await ctx.respond(
                embed=_alert(
                    self.default_persona.messages.interface.mdc.alert_title,
                    self.default_persona.messages.interface.mdc.operation_failed,
                ),
                ephemeral=True,
            )

    async def search(
        self,
        interaction: discord.Interaction,
        requester_id: int,
        raw_name: str,
    ) -> None:
        if (
            interaction.guild is None
            or interaction.channel is None
            or not isinstance(interaction.user, discord.Member)
            or interaction.user.id != requester_id
        ):
            await interaction.response.send_message(
                embed=_generic_alert("La consulta ya no es válida."),
                ephemeral=True,
            )
            return
        guild_id = interaction.guild.id
        user_id = interaction.user.id
        try:
            bundle = await self.provider.cached(guild_id)
            ui = bundle.persona.profile.messages.interface.mdc
            if not _member_has_any_role(
                interaction.user,
                bundle.settings.operational_staff_role_ids,
            ):
                await interaction.response.send_message(
                    embed=_alert(ui.alert_title, ui.unauthorized),
                    ephemeral=True,
                )
                return
            normalized_name = normalize_character_name(raw_name)
            await interaction.response.defer(ephemeral=True)
            record = await self.service.lookup_exact(normalized_name)
            await interaction.channel.send(
                embed=build_mdc_embed(record, ui),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            with suppress(discord.HTTPException):
                await interaction.delete_original_response()
            log_event(
                LOGGER,
                "info",
                "mdc_lookup_published",
                guild_id=guild_id,
                channel_id=interaction.channel.id,
                user_id=user_id,
                partial=bool(record.unavailable_sections),
                unavailable_sections=[section.value for section in record.unavailable_sections],
            )
        except MdcCharacterNotFoundError:
            await self._send_lookup_error(
                interaction,
                ui,
                ui.not_found.format(name=_safe_name(raw_name)),
            )
        except MdcCharacterAmbiguousError:
            await self._send_lookup_error(
                interaction,
                ui,
                ui.ambiguous.format(name=_safe_name(raw_name)),
            )
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "mdc_lookup_failed",
                guild_id=guild_id,
                user_id=user_id,
                exc_info=error,
                **exception_fields(error),
            )
            await self._send_lookup_error(interaction, ui, ui.operation_failed)

    @staticmethod
    async def _send_lookup_error(
        interaction: discord.Interaction,
        ui: PersonaMdcMessages,
        description: str,
    ) -> None:
        embed = _alert(ui.alert_title, description)
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)


class MdcSearchModal(discord.ui.DesignerModal):
    """Collect an exact character name without slash-command arguments."""

    def __init__(self, cog: MdcCog, requester_id: int) -> None:
        self.cog = cog
        self.requester_id = requester_id
        self.name = discord.ui.InputText(
            custom_id="mdc:v1:name",
            placeholder="Ej.: Nombre Apellido",
            min_length=3,
            max_length=100,
        )
        super().__init__(
            discord.ui.Label("Nombre exacto del personaje", self.name),
            title="Consultar MDC",
            custom_id=f"mdc:v1:search:{requester_id}",
            timeout=MODAL_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.search(interaction, self.requester_id, self.name.value or "")


def build_mdc_embed(record: MdcRecord, ui: PersonaMdcMessages) -> discord.Embed:
    """Render a bounded public record without leaking transport identifiers or OOC fields."""

    safe_name = _safe_name(record.name)
    embed = discord.Embed(
        title=f"🖥️ MDC · {safe_name}",
        description=ui.result_intro.format(name=safe_name),
        color=_record_color(record),
        timestamp=datetime.now(UTC),
    )
    if MdcSection.STATUS not in record.unavailable_sections:
        embed.add_field(
            name="📌 Estado",
            value="🔒 En custodia" if record.incarcerated else "🟢 Sin custodia vigente",
            inline=False,
        )
    if MdcSection.WARRANTS not in record.unavailable_sections:
        embed.add_field(
            name=f"📋 Órdenes activas · {record.active_warrant_count or 0}",
            value=_format_warrants(record.active_warrants),
            inline=False,
        )
    if MdcSection.FINES not in record.unavailable_sections:
        embed.add_field(
            name=f"💵 Multas pendientes · {record.pending_fine_count or 0}",
            value=_format_fines(record),
            inline=False,
        )
    if MdcSection.POSSESSIONS not in record.unavailable_sections:
        embed.add_field(
            name=f"🚘 Vehículos registrados · {len(record.vehicles)}",
            value=_format_vehicles(record),
            inline=False,
        )
        embed.add_field(
            name=f"🏠 Propiedades registradas · {len(record.properties)}",
            value=_format_properties(record),
            inline=False,
        )
    if MdcSection.ARRESTS not in record.unavailable_sections:
        embed.add_field(
            name=f"📁 Antecedentes de arresto · {record.arrest_count or 0}",
            value=_format_arrests(record.arrests),
            inline=False,
        )
    if record.unavailable_sections:
        sections = ", ".join(SECTION_LABELS[section] for section in record.unavailable_sections)
        embed.add_field(
            name="⚠️ Información no disponible",
            value=ui.partial_warning.format(sections=sections),
            inline=False,
        )
    embed.set_footer(text="Consulta MDC · información operativa")
    return embed


def _format_warrants(warrants: tuple[MdcWarrant, ...]) -> str:
    if not warrants:
        return "Sin órdenes activas registradas."
    return "\n".join(_format_warrant(item) for item in warrants[:MAX_LISTED_ITEMS])[:1024]


def _format_warrant(warrant: MdcWarrant) -> str:
    charges = _charge_names(warrant.charges)
    parts = [charges or _clean(warrant.description) or "Orden activa"]
    if warrant.risk:
        parts.append(f"riesgo: {_clean(warrant.risk)}")
    return f"• {' · '.join(parts)}"


def _format_fines(record: MdcRecord) -> str:
    if not record.pending_fines:
        return "Sin multas impagas registradas."
    lines = [f"• {_clean(item.description)} · {_money(item.cost)}" for item in record.pending_fines]
    if record.pending_fine_amount is not None:
        lines.append(f"**Total pendiente:** {_money(record.pending_fine_amount)}")
    elif (record.pending_fine_count or 0) > len(record.pending_fines):
        lines.append("Se muestran las multas más recientes.")
    return "\n".join(lines)[:1024]


def _format_vehicles(record: MdcRecord) -> str:
    if not record.vehicles:
        return "Sin vehículos registrados."
    lines = [
        f"• {_clean(item.name)}" + (f" · `{_clean(item.plate)}`" if item.plate else "")
        for item in record.vehicles[:MAX_LISTED_ITEMS]
    ]
    return _with_remaining(lines, len(record.vehicles))


def _format_properties(record: MdcRecord) -> str:
    if not record.properties:
        return "Sin propiedades registradas."
    lines = [
        f"• {_clean(item.name)}" + (f" · {_clean(item.address)}" if item.address else "")
        for item in record.properties[:MAX_LISTED_ITEMS]
    ]
    return _with_remaining(lines, len(record.properties))


def _format_arrests(arrests: tuple[MdcArrest, ...]) -> str:
    if not arrests:
        return "Sin arrestos registrados."
    lines = []
    for arrest in arrests[:MAX_LISTED_ITEMS]:
        date = discord.utils.format_dt(arrest.created_at, "d") if arrest.created_at else "Sin fecha"
        lines.append(f"• {date} · {_charge_names(arrest.charges) or 'Sin cargos detallados'}")
    return "\n".join(lines)[:1024]


def _charge_names(charges: tuple[object, ...]) -> str:
    names = [_clean(getattr(charge, "name", "")) for charge in charges[:3]]
    return ", ".join(name for name in names if name)


def _with_remaining(lines: list[str], total: int) -> str:
    remaining = total - len(lines)
    if remaining > 0:
        lines.append(f"… y {remaining} más.")
    return "\n".join(lines)[:1024]


def _money(value: int | float) -> str:
    return f"${value:,.0f}".replace(",", ".")


def _safe_name(value: str) -> str:
    return discord.utils.escape_markdown(normalize_character_name(value))[:100] or "Sin nombre"


def _clean(value: object) -> str:
    return discord.utils.escape_markdown(str(value).strip())


def _record_color(record: MdcRecord) -> discord.Color:
    if (record.active_warrant_count or 0) > 0 or record.incarcerated:
        return discord.Color.red()
    if record.unavailable_sections:
        return discord.Color.orange()
    return discord.Color.blurple()


def _member_has_any_role(member: discord.Member, role_ids: tuple[int, ...]) -> bool:
    allowed = set(role_ids)
    return bool(allowed and any(role.id in allowed for role in member.roles))


def _alert(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=discord.Color.red())


def _generic_alert(description: str) -> discord.Embed:
    return _alert("⚠️ MDC no disponible", description)
