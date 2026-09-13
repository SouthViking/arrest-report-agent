"""Discord command and compact presentation for unfinished arrest reports."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime
from enum import StrEnum
from math import ceil

import discord
from discord.ext import commands

from arrest_report_agent.application.date_ranges import DateRangePeriod, resolve_date_range
from arrest_report_agent.config import BotPersona, PersonaPendingArrestReportsMessages
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider

from .models import (
    ArrestReviewStatus,
    PendingArrestReport,
    PendingArrestReportScan,
    PendingReportScanTooLargeError,
    PendingReportStatus,
)
from .service import PendingArrestReportService

LOGGER = logging.getLogger(__name__)
MODAL_TIMEOUT_SECONDS = 300
VIEW_TIMEOUT_SECONDS = 300
# Keep each slice bounded so worst-case names and headings remain below Discord's limit.
REPORTS_PER_PAGE = 8


class PendingReportVisibility(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"


PendingReportPeriod = DateRangePeriod


class PendingArrestReportCog(commands.Cog):
    """Expose one manual, role-gated scan without coupling it to scheduling."""

    def __init__(
        self,
        provider: DatabaseConfigProvider,
        service: PendingArrestReportService,
        persona: BotPersona,
    ) -> None:
        self.provider = provider
        self.service = service
        self.default_persona = persona
        self._guild_locks: dict[int, asyncio.Lock] = {}
        self.pending_arrest_reports.description = (
            persona.messages.interface.commands.pending_arrest_reports_description
        )

    @discord.slash_command(
        name="reportes-pendientes",
        description="Detecta reportes sin PCU completo o revisión aprobada",
    )
    async def pending_arrest_reports(self, ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            await ctx.respond(
                embed=_generic_alert("Este comando solo funciona dentro de un servidor."),
                ephemeral=True,
            )
            return
        try:
            bundle = await self.provider.cached(ctx.guild.id)
            ui = bundle.persona.profile.messages.interface.pending_arrest_reports
            if not _member_has_any_role(
                ctx.author,
                bundle.settings.operational_staff_role_ids,
            ):
                await ctx.respond(embed=_alert(ui.alert_title, ui.unauthorized), ephemeral=True)
                return
            if bundle.settings.pending_arrest_reports_faction_id is None:
                await ctx.respond(embed=_alert(ui.alert_title, ui.unavailable), ephemeral=True)
                return
            await ctx.send_modal(PendingArrestReportModal(self, ctx.author.id))
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "pending_arrest_reports_command_failed",
                guild_id=ctx.guild.id,
                user_id=ctx.author.id,
                exc_info=error,
                **exception_fields(error),
            )
            ui = self.default_persona.messages.interface.pending_arrest_reports
            await ctx.respond(embed=_alert(ui.alert_title, ui.operation_failed), ephemeral=True)

    async def scan(
        self,
        interaction: discord.Interaction,
        *,
        requester_id: int,
        period: PendingReportPeriod,
        raw_date_from: str,
        raw_date_to: str,
        visibility: PendingReportVisibility,
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
        bundle = await self.provider.cached(guild_id)
        ui = bundle.persona.profile.messages.interface.pending_arrest_reports
        if not _member_has_any_role(
            interaction.user,
            bundle.settings.operational_staff_role_ids,
        ):
            await interaction.response.send_message(
                embed=_alert(ui.alert_title, ui.unauthorized),
                ephemeral=True,
            )
            return
        faction_id = bundle.settings.pending_arrest_reports_faction_id
        if faction_id is None:
            await interaction.response.send_message(
                embed=_alert(ui.alert_title, ui.unavailable),
                ephemeral=True,
            )
            return
        try:
            date_from, date_to = _resolve_date_range(
                period,
                raw_date_from,
                raw_date_to,
                max_days=bundle.settings.pending_arrest_reports_max_range_days,
            )
        except ValueError:
            await interaction.response.send_message(
                embed=_alert(ui.alert_title, ui.invalid_range),
                ephemeral=True,
            )
            return

        private = visibility is PendingReportVisibility.PRIVATE
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
                result = await self.service.scan(
                    faction_id=faction_id,
                    date_from=date_from,
                    date_to=date_to,
                    max_records=bundle.settings.pending_arrest_reports_max_records,
                    guild_id=guild_id,
                    report_channel_ids=bundle.settings.report_channel_allowlist.ids,
                    authorized_reviewer_role_ids=(
                        bundle.settings.supervisor_role_allowlist.ids
                    ),
                )
            view = PendingReportPaginationView(
                result=result,
                ui=ui,
            )
            if view.page_count > 1:
                await interaction.followup.send(
                    embed=view.current_embed,
                    view=view,
                    ephemeral=private,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                # Pycord treats an explicit None as a supplied view and dereferences
                # its timeout for ephemeral webhook responses. Omit it entirely.
                await interaction.followup.send(
                    embed=view.current_embed,
                    ephemeral=private,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            log_event(
                LOGGER,
                "info",
                "pending_arrest_reports_published",
                guild_id=guild_id,
                channel_id=interaction.channel.id,
                user_id=user_id,
                visibility=visibility.value,
                date_from=date_from.isoformat(),
                date_to=date_to.isoformat(),
                total_reports=result.total_reports,
                pending_reports=result.pending_count,
            )
        except PendingReportScanTooLargeError as error:
            await interaction.followup.send(
                embed=_alert(
                    ui.alert_title,
                    ui.too_many_reports.format(limit=error.limit),
                ),
                ephemeral=private,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "pending_arrest_reports_scan_failed",
                guild_id=guild_id,
                channel_id=interaction.channel.id,
                user_id=user_id,
                exc_info=error,
                **exception_fields(error),
            )
            await interaction.followup.send(
                embed=_alert(ui.alert_title, ui.operation_failed),
                ephemeral=private,
                allowed_mentions=discord.AllowedMentions.none(),
            )


class PendingArrestReportModal(discord.ui.DesignerModal):
    """Collect a bounded date range and result visibility."""

    def __init__(self, cog: PendingArrestReportCog, requester_id: int) -> None:
        self.cog = cog
        self.requester_id = requester_id
        self.period = discord.ui.StringSelect(  # type: ignore[type-var]
            custom_id="pending_arrests:v1:period",
            options=[
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
            custom_id="pending_arrests:v1:date_from",
            placeholder="AAAA-MM-DD",
            required=False,
            min_length=10,
            max_length=10,
        )
        self.date_to = discord.ui.InputText(
            custom_id="pending_arrests:v1:date_to",
            placeholder="AAAA-MM-DD",
            required=False,
            min_length=10,
            max_length=10,
        )
        self.visibility = discord.ui.StringSelect(  # type: ignore[type-var]
            custom_id="pending_arrests:v1:visibility",
            options=[
                discord.SelectOption(
                    label="Visible para todos",
                    value=PendingReportVisibility.PUBLIC.value,
                    emoji="🌐",
                    default=True,
                ),
                discord.SelectOption(
                    label="Solo para mí",
                    value=PendingReportVisibility.PRIVATE.value,
                    emoji="🔒",
                ),
            ],
            min_values=1,
            max_values=1,
        )
        super().__init__(
            discord.ui.Label("Período", self.period),
            discord.ui.Label("Desde · solo para rango personalizado", self.date_from),
            discord.ui.Label("Hasta · solo para rango personalizado", self.date_to),
            discord.ui.Label("Quién puede ver el resultado", self.visibility),
            title="Buscar reportes pendientes",
            custom_id=f"pending_arrests:v1:scan:{requester_id}",
            timeout=MODAL_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        period = next(iter(self.period.values or ()), PendingReportPeriod.LAST_7_DAYS.value)
        visibility = next(
            iter(self.visibility.values or ()),
            PendingReportVisibility.PUBLIC.value,
        )
        await self.cog.scan(
            interaction,
            requester_id=self.requester_id,
            period=PendingReportPeriod(period),
            raw_date_from=self.date_from.value or "",
            raw_date_to=self.date_to.value or "",
            visibility=PendingReportVisibility(visibility),
        )


class PendingReportPaginationView(discord.ui.View):
    def __init__(
        self,
        *,
        result: PendingArrestReportScan,
        ui: PersonaPendingArrestReportsMessages,
    ) -> None:
        super().__init__(timeout=VIEW_TIMEOUT_SECONDS, disable_on_timeout=True)
        self.result = result
        self.ui = ui
        self.page = 0
        self._synchronize_buttons()

    @property
    def page_count(self) -> int:
        return max(1, ceil(self.result.pending_count / REPORTS_PER_PAGE))

    @property
    def current_embed(self) -> discord.Embed:
        return build_pending_arrest_reports_embed(
            self.result,
            ui=self.ui,
            page=self.page,
        )

    @discord.ui.button(label="Anterior", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous_page(
        self,
        _button: discord.ui.Button,
        interaction: discord.Interaction,
    ) -> None:
        self.page = max(0, self.page - 1)
        self._synchronize_buttons()
        await interaction.response.edit_message(embed=self.current_embed, view=self)

    @discord.ui.button(label="Siguiente", emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next_page(
        self,
        _button: discord.ui.Button,
        interaction: discord.Interaction,
    ) -> None:
        self.page = min(self.page_count - 1, self.page + 1)
        self._synchronize_buttons()
        await interaction.response.edit_message(embed=self.current_embed, view=self)

    def _synchronize_buttons(self) -> None:
        self.previous_page.disabled = self.page <= 0
        self.next_page.disabled = self.page >= self.page_count - 1


def build_pending_arrest_reports_embed(
    result: PendingArrestReportScan,
    *,
    ui: PersonaPendingArrestReportsMessages,
    page: int = 0,
) -> discord.Embed:
    """Render one compact, single-list embed for every incomplete workflow."""

    ordered_reports = _group_reports_by_officer(result.pending_reports)
    page_count = max(1, ceil(len(ordered_reports) / REPORTS_PER_PAGE))
    bounded_page = min(max(page, 0), page_count - 1)
    start = bounded_page * REPORTS_PER_PAGE
    reports = ordered_reports[start : start + REPORTS_PER_PAGE]
    summary = (
        f"**{_format_date(result.date_from)} — {_format_date(result.date_to)}** · "
        f"{result.pending_count} de {result.total_reports} pendientes"
    )
    description_parts = [summary]
    if reports:
        description_parts.append(
            _format_grouped_report_entries(
                reports,
                ordered_reports=ordered_reports,
                page_start=start,
            )
        )
    else:
        description_parts.append(ui.no_pending)
    description = "\n\n".join(description_parts)
    embed = discord.Embed(
        title="⏳ Reportes de arresto pendientes",
        description=description,
        color=_embed_color(result.pending_reports),
        timestamp=datetime.now(UTC),
    )
    if any(
        report.pcu_reason is not None
        and report.status is PendingReportStatus.OVER_NORMAL_LIMIT
        for report in result.pending_reports
    ):
        embed.description = (
            f"{embed.description}\n\n{ui.extension_warning}"
        )
    if page_count > 1:
        embed.set_footer(text=f"Página {bounded_page + 1} de {page_count}")
    return embed


def _group_reports_by_officer(
    reports: tuple[PendingArrestReport, ...],
) -> tuple[PendingArrestReport, ...]:
    grouped: dict[str, list[PendingArrestReport]] = {}
    for report in reports:
        grouped.setdefault(_officer_key(report), []).append(report)
    ordered_groups = sorted(
        grouped.items(),
        key=lambda item: (-len(item[1]), item[0]),
    )
    ordered = tuple(
        report
        for _, group in ordered_groups
        for report in sorted(group, key=lambda item: (item.created_at, item.report_id))
    )
    return ordered


def _format_grouped_report_entries(
    reports: tuple[PendingArrestReport, ...],
    *,
    ordered_reports: tuple[PendingArrestReport, ...],
    page_start: int,
) -> str:
    previous_key = (
        _officer_key(ordered_reports[page_start - 1])
        if page_start > 0 and ordered_reports
        else None
    )
    sections: list[str] = []
    current_key: str | None = None
    current_lines: list[str] = []
    for report in reports:
        key = _officer_key(report)
        if key != current_key:
            if current_lines:
                sections.append("\n".join(current_lines))
            suffix = " · continuación" if not sections and key == previous_key else ""
            current_lines = [
                f"### 👮 {_compact_name(report.officer_name, limit=70)}{suffix}"
            ]
            current_key = key
        current_lines.append(_format_report_entry(report))
    if current_lines:
        sections.append("\n".join(current_lines))
    return "\n\n".join(sections)


def _format_report_entry(report: PendingArrestReport) -> str:
    suspect = _compact_name(report.suspect_name)
    arrest_date = f"<t:{int(report.created_at.timestamp())}:R>"
    reasons: list[str] = []
    if report.pcu_reason is not None:
        reasons.append("Sin PCU")
    if report.review_status is ArrestReviewStatus.MISSING:
        reasons.append("Sin revisión")
    elif report.review_status is ArrestReviewStatus.PENDING:
        reasons.append("Revisión pendiente")
    if report.review_url is not None:
        reasons.append(f"[Post]({report.review_url})")
    return (
        f"• **#{report.report_id}** · {suspect} · {arrest_date} — "
        f"{' · '.join(reasons)}"
    )


def _officer_key(report: PendingArrestReport) -> str:
    return " ".join(report.officer_name.split()).casefold()


def _compact_name(value: str, *, limit: int = 70) -> str:
    escaped = discord.utils.escape_markdown(value)
    return escaped if len(escaped) <= limit else f"{escaped[: limit - 1].rstrip()}…"


def _resolve_date_range(
    period: PendingReportPeriod,
    raw_date_from: str,
    raw_date_to: str,
    *,
    max_days: int,
) -> tuple[date, date]:
    return resolve_date_range(
        period,
        raw_date_from,
        raw_date_to,
        max_days=max_days,
    )


def _embed_color(
    reports: tuple[PendingArrestReport, ...],
) -> discord.Color:
    statuses = {report.status for report in reports}
    if PendingReportStatus.OVER_MAXIMUM_LIMIT in statuses:
        return discord.Color.red()
    if PendingReportStatus.OVER_NORMAL_LIMIT in statuses:
        return discord.Color.orange()
    if PendingReportStatus.NEAR_DEADLINE in statuses:
        return discord.Color.gold()
    if reports:
        return discord.Color.gold()
    return discord.Color.green()


def _format_date(value: date) -> str:
    months = (
        "enero",
        "febrero",
        "marzo",
        "abril",
        "mayo",
        "junio",
        "julio",
        "agosto",
        "septiembre",
        "octubre",
        "noviembre",
        "diciembre",
    )
    return f"{value.day} {months[value.month - 1]} {value.year}"


def _member_has_any_role(member: discord.Member, role_ids: tuple[int, ...]) -> bool:
    allowed = set(role_ids)
    return bool(allowed and any(role.id in allowed for role in member.roles))


def _alert(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=discord.Color.red())


def _generic_alert(description: str) -> discord.Embed:
    return _alert("⚠️ Consulta no disponible", description)
