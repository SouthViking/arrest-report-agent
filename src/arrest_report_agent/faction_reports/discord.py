"""Visibility-aware Discord workflow for restricted faction XLSX reports."""

from __future__ import annotations

import asyncio
import io
import logging
from enum import StrEnum

import discord
from discord.ext import commands

from arrest_report_agent.application.date_ranges import DateRangePeriod, resolve_date_range
from arrest_report_agent.config import BotPersona, PersonaFactionReportMessages
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider

from .models import FactionReport, FactionReportTooLargeError
from .service import FactionReportService
from .workbook import build_faction_report_workbook

LOGGER = logging.getLogger(__name__)
MODAL_TIMEOUT_SECONDS = 300


class FactionReportVisibility(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"


class FactionReportCog(commands.Cog):
    """Generate one privacy-minimized report for explicitly authorized staff."""

    def __init__(
        self,
        provider: DatabaseConfigProvider,
        service: FactionReportService,
        persona: BotPersona,
    ) -> None:
        self.provider = provider
        self.service = service
        self.default_persona = persona
        self._guild_locks: dict[int, asyncio.Lock] = {}
        self.faction_report.description = (
            persona.messages.interface.commands.faction_report_description
        )

    @discord.slash_command(
        name="informe-faccion",
        description="Genera el informe restringido de banco y miembros",
    )
    async def faction_report(self, ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            await ctx.respond(
                embed=_alert(
                    "⚠️ Informe no disponible",
                    "Este comando solo funciona dentro de un servidor.",
                ),
                ephemeral=True,
            )
            return
        try:
            bundle = await self.provider.cached(ctx.guild.id)
            ui = bundle.persona.profile.messages.interface.faction_reports
            if not _member_has_any_role(ctx.author, bundle.settings.faction_report_role_ids):
                await ctx.respond(embed=_alert(ui.alert_title, ui.unauthorized), ephemeral=True)
                return
            if bundle.settings.faction_report_options is None:
                await ctx.respond(embed=_alert(ui.alert_title, ui.unavailable), ephemeral=True)
                return
            await ctx.send_modal(FactionReportModal(self, requester_id=ctx.author.id))
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "faction_report_command_failed",
                guild_id=ctx.guild.id,
                user_id=ctx.author.id,
                exc_info=error,
                **exception_fields(error),
            )
            ui = self.default_persona.messages.interface.faction_reports
            await ctx.respond(embed=_alert(ui.alert_title, ui.operation_failed), ephemeral=True)

    async def generate(
        self,
        interaction: discord.Interaction,
        *,
        requester_id: int,
        period: DateRangePeriod,
        raw_date_from: str,
        raw_date_to: str,
        visibility: FactionReportVisibility,
    ) -> None:
        if (
            interaction.guild is None
            or interaction.channel is None
            or not isinstance(interaction.user, discord.Member)
            or interaction.user.id != requester_id
        ):
            await interaction.response.send_message(
                embed=_alert("⚠️ Informe no disponible", "Esta consulta ya no es válida."),
                ephemeral=True,
            )
            return
        guild_id = interaction.guild.id
        bundle = await self.provider.cached(guild_id)
        ui = bundle.persona.profile.messages.interface.faction_reports
        if not _member_has_any_role(interaction.user, bundle.settings.faction_report_role_ids):
            await interaction.response.send_message(
                embed=_alert(ui.alert_title, ui.unauthorized),
                ephemeral=True,
            )
            return
        options = bundle.settings.faction_report_options
        if options is None:
            await interaction.response.send_message(
                embed=_alert(ui.alert_title, ui.unavailable),
                ephemeral=True,
            )
            return
        try:
            date_from, date_to = resolve_date_range(
                period,
                raw_date_from,
                raw_date_to,
                max_days=options.max_range_days,
            )
        except ValueError:
            await interaction.response.send_message(
                embed=_alert(ui.alert_title, ui.invalid_range),
                ephemeral=True,
            )
            return
        private = visibility is FactionReportVisibility.PRIVATE
        lock = self._guild_locks.setdefault(guild_id, asyncio.Lock())
        if lock.locked():
            await interaction.response.send_message(
                embed=_alert(ui.alert_title, ui.busy),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=private)
        try:
            async with lock:
                report = await self.service.generate(
                    faction_id=options.faction_id,
                    date_from=date_from,
                    date_to=date_to,
                    max_bank_records=options.max_bank_records,
                )
                workbook = await asyncio.to_thread(
                    build_faction_report_workbook,
                    report,
                    organization_name=bundle.persona.profile.identity.organization_name,
                )
            filename = f"informe-faccion-{date_from.isoformat()}-{date_to.isoformat()}.xlsx"
            await interaction.followup.send(
                embed=_ready_embed(report, ui),
                file=discord.File(io.BytesIO(workbook), filename=filename),
                ephemeral=private,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log_event(
                LOGGER,
                "info",
                "faction_report_generated",
                guild_id=guild_id,
                user_id=interaction.user.id,
                faction_id=options.faction_id,
                date_from=date_from.isoformat(),
                date_to=date_to.isoformat(),
                bank_movements=len(report.bank_entries),
                active_members=len(report.active_members),
                workbook_bytes=len(workbook),
                visibility=visibility.value,
            )
        except FactionReportTooLargeError as error:
            await interaction.followup.send(
                embed=_alert(
                    ui.alert_title,
                    ui.too_many_records.format(limit=error.limit),
                ),
                ephemeral=private,
            )
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "faction_report_generation_failed",
                guild_id=guild_id,
                user_id=interaction.user.id,
                faction_id=options.faction_id,
                date_from=date_from.isoformat(),
                date_to=date_to.isoformat(),
                exc_info=error,
                **exception_fields(error),
            )
            await interaction.followup.send(
                embed=_alert(ui.alert_title, ui.operation_failed),
                ephemeral=private,
            )


class FactionReportModal(discord.ui.DesignerModal):
    """Collect a quick preset or explicit inclusive bank period."""

    def __init__(self, cog: FactionReportCog, requester_id: int) -> None:
        self.cog = cog
        self.requester_id = requester_id
        self.period = discord.ui.StringSelect(  # type: ignore[type-var]
            custom_id="faction_report:v1:period",
            options=[
                discord.SelectOption(label="Último día", value="1"),
                discord.SelectOption(label="Últimos 2 días", value="2"),
                discord.SelectOption(label="Últimos 7 días", value="7", default=True),
                discord.SelectOption(label="Últimos 14 días", value="14"),
                discord.SelectOption(label="Últimos 30 días", value="30"),
                discord.SelectOption(label="Últimos 90 días", value="90"),
                discord.SelectOption(label="Rango personalizado", value="custom"),
            ],
            min_values=1,
            max_values=1,
        )
        self.date_from = discord.ui.InputText(
            custom_id="faction_report:v1:date_from",
            placeholder="AAAA-MM-DD",
            required=False,
            min_length=10,
            max_length=10,
        )
        self.date_to = discord.ui.InputText(
            custom_id="faction_report:v1:date_to",
            placeholder="AAAA-MM-DD",
            required=False,
            min_length=10,
            max_length=10,
        )
        self.publish = discord.ui.Checkbox(
            custom_id="faction_report:v2:publish",
            default=False,
        )
        super().__init__(
            discord.ui.Label("Período bancario", self.period),
            discord.ui.Label("Desde · solo para rango personalizado", self.date_from),
            discord.ui.Label("Hasta · solo para rango personalizado", self.date_to),
            discord.ui.Label("Publicar el informe en este canal", self.publish),
            title="Generar informe de facción",
            custom_id=f"faction_report:v2:generate:{requester_id}",
            timeout=MODAL_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        period = next(iter(self.period.values or ()), DateRangePeriod.LAST_7_DAYS.value)
        await self.cog.generate(
            interaction,
            requester_id=self.requester_id,
            period=DateRangePeriod(period),
            raw_date_from=self.date_from.value or "",
            raw_date_to=self.date_to.value or "",
            visibility=(
                FactionReportVisibility.PUBLIC
                if self.publish.value
                else FactionReportVisibility.PRIVATE
            ),
        )


def _ready_embed(
    report: FactionReport,
    ui: PersonaFactionReportMessages,
) -> discord.Embed:
    return discord.Embed(
        title=ui.ready_title,
        description=ui.ready_description.format(
            date_from=report.date_from.isoformat(),
            date_to=report.date_to.isoformat(),
            movements=len(report.bank_entries),
            members=len(report.active_members),
        ),
        color=discord.Color.dark_blue(),
    )


def _member_has_any_role(member: discord.Member, role_ids: tuple[int, ...]) -> bool:
    allowed = set(role_ids)
    return bool(allowed and any(role.id in allowed for role in member.roles))


def _alert(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=discord.Color.red())
