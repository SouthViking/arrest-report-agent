"""Deterministic Discord history queries and private UI for personal reports."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from math import ceil

import discord

from arrest_report_agent.application import (
    PersonalReport,
    PersonalReportPeriod,
    PersonalReportQuery,
    PersonalReportSearchResult,
    PersonalReportStatus,
    PersonalReportStatusFilter,
    PersonalReportSummary,
)
from arrest_report_agent.config import AppSettings
from arrest_report_agent.config.logging import exception_fields, log_event

LOGGER = logging.getLogger(__name__)

MAX_HISTORY_MESSAGES_PER_CHANNEL = 500
REPORTS_PER_PAGE = 5
MONTH_NAMES = (
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
PERIOD_LABELS = {
    PersonalReportPeriod.CURRENT_MONTH: "Mes actual",
    PersonalReportPeriod.LAST_7_DAYS: "Últimos 7 días",
    PersonalReportPeriod.LAST_30_DAYS: "Últimos 30 días",
    PersonalReportPeriod.LAST_90_DAYS: "Últimos 90 días",
}
STATUS_FILTER_LABELS = {
    PersonalReportStatusFilter.ALL: "Todos los estados",
    PersonalReportStatusFilter.PENDING: "Pendientes",
    PersonalReportStatusFilter.APPROVED: "Aprobados",
    PersonalReportStatusFilter.CORRECTIONS_REQUIRED: "Con correcciones",
    PersonalReportStatusFilter.INCONCLUSIVE: "Requieren supervisión",
}
STATUS_PRESENTATION = {
    PersonalReportStatus.PENDING: ("⏳", "Pendiente"),
    PersonalReportStatus.IN_REVIEW: ("🔎", "En revisión"),
    PersonalReportStatus.APPROVED: ("✅", "Aprobado"),
    PersonalReportStatus.CORRECTIONS_REQUIRED: ("❌", "Con correcciones"),
    PersonalReportStatus.INCONCLUSIVE: ("⚠️", "Requiere supervisión"),
}
BOT_STATUS_REACTIONS = {
    "⏳": PersonalReportStatus.IN_REVIEW,
    "✅": PersonalReportStatus.APPROVED,
    "❌": PersonalReportStatus.CORRECTIONS_REQUIRED,
    "⚠️": PersonalReportStatus.INCONCLUSIVE,
}


class DiscordPersonalReportFinder:
    """Read a bounded snapshot from configured report channels without invoking AI."""

    def __init__(self, bot: discord.Bot, settings: AppSettings) -> None:
        self.bot = bot
        self.settings = settings

    def visible_channels(
        self,
        guild: discord.Guild,
        member: discord.Member,
    ) -> tuple[discord.TextChannel, ...]:
        configured_ids = self.settings.channel_allowlist.ids
        if configured_ids is None:
            candidates = tuple(guild.text_channels)
        else:
            candidates = tuple(
                channel
                for channel_id in sorted(configured_ids)
                if (channel := guild.get_channel(channel_id)) is not None
            )

        bot_member = guild.me
        visible: list[discord.TextChannel] = []
        for channel in candidates:
            if not isinstance(channel, discord.TextChannel):
                continue
            if not _can_read_history(channel, member):
                continue
            if bot_member is not None and not _can_read_history(channel, bot_member):
                continue
            visible.append(channel)
        return tuple(sorted(visible, key=lambda channel: (channel.position, channel.id)))

    async def find(
        self,
        guild: discord.Guild,
        member: discord.Member,
        query: PersonalReportQuery,
        *,
        now: datetime | None = None,
    ) -> PersonalReportSearchResult:
        reference_time = now or datetime.now(UTC)
        after = query.period.starts_at(reference_time)
        channels = self.visible_channels(guild, member)
        if query.channel_id is not None:
            channels = tuple(channel for channel in channels if channel.id == query.channel_id)

        reports: list[PersonalReport] = []
        truncated = False
        for channel in channels:
            scanned = 0
            try:
                async for message in channel.history(
                    limit=MAX_HISTORY_MESSAGES_PER_CHANNEL,
                    after=after,
                    oldest_first=False,
                ):
                    scanned += 1
                    status = _status_from_bot_reactions(message)
                    if not _is_report_candidate(message, member.id, status):
                        continue
                    if not query.status.includes(status):
                        continue
                    reports.append(_to_personal_report(message, status, guild.id))
            except (discord.Forbidden, discord.HTTPException) as error:
                log_event(
                    LOGGER,
                    "warning",
                    "personal_report_history_unavailable",
                    exc_info=error,
                    guild_id=guild.id,
                    channel_id=channel.id,
                    user_id=member.id,
                    **exception_fields(error),
                )
                continue
            if scanned >= MAX_HISTORY_MESSAGES_PER_CHANNEL:
                truncated = True

        reports.sort(key=lambda report: (report.created_at, report.message_id), reverse=True)
        return PersonalReportSearchResult(reports=tuple(reports), truncated=truncated)


class PersonalReportsModal(discord.ui.DesignerModal):
    """Single-user filter modal for `/wv mis-reportes`."""

    def __init__(
        self,
        *,
        finder: DiscordPersonalReportFinder,
        guild: discord.Guild,
        member: discord.Member,
        channels: tuple[discord.TextChannel, ...],
        display_name: str,
    ) -> None:
        self.finder = finder
        self.guild = guild
        self.member = member
        self.display_name = display_name
        self.period_select = discord.ui.StringSelect(
            custom_id="personal_reports_period",
            options=[
                discord.SelectOption(
                    label=label,
                    value=period.value,
                    default=period is PersonalReportPeriod.CURRENT_MONTH,
                )
                for period, label in PERIOD_LABELS.items()
            ],
        )
        self.status_select = discord.ui.StringSelect(
            custom_id="personal_reports_status",
            options=[
                discord.SelectOption(
                    label=label,
                    value=status.value,
                    default=status is PersonalReportStatusFilter.ALL,
                )
                for status, label in STATUS_FILTER_LABELS.items()
            ],
        )
        channel_options = [
            discord.SelectOption(label="Todas las estaciones", value="all", default=True)
        ]
        channel_options.extend(
            discord.SelectOption(
                label=f"#{channel.name}"[:100],
                value=str(channel.id),
            )
            for channel in channels[:24]
        )
        self.channel_select = discord.ui.StringSelect(
            custom_id="personal_reports_channel",
            options=channel_options,
        )
        super().__init__(
            discord.ui.Label(
                "Periodo",
                self.period_select,
                description="Acota la búsqueda para evitar recorridos innecesarios.",
            ),
            discord.ui.Label("Estado actual", self.status_select),
            discord.ui.Label("Estación", self.channel_select),
            title="Consultar mis reportes",
            custom_id=f"personal_reports:{member.id}",
            timeout=180,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.member.id:
            await interaction.response.send_message(
                "Esta consulta pertenece a otro miembro.",
                ephemeral=True,
            )
            return

        period = PersonalReportPeriod(self.period_select.values[0])
        status = PersonalReportStatusFilter(self.status_select.values[0])
        raw_channel_id = self.channel_select.values[0]
        query = PersonalReportQuery(
            period=period,
            status=status,
            channel_id=None if raw_channel_id == "all" else int(raw_channel_id),
        )
        await interaction.response.defer(ephemeral=True)
        try:
            result = await self.finder.find(self.guild, self.member, query)
            view = PersonalReportPaginationView(
                requester_id=self.member.id,
                result=result,
                query=query,
                display_name=self.display_name,
            )
            await send_personal_report_result(interaction, view)
            log_event(
                LOGGER,
                "info",
                "personal_report_query_completed",
                guild_id=self.guild.id,
                user_id=self.member.id,
                period=period.value,
                status=status.value,
                channel_id=query.channel_id or "all",
                result_count=len(result.reports),
                truncated=result.truncated,
            )
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "personal_report_query_failed",
                exc_info=error,
                guild_id=self.guild.id,
                user_id=self.member.id,
                interaction_id=interaction.id,
                **exception_fields(error),
            )
            await interaction.followup.send(
                "📁 No pude consultar el archivo de reportes en este momento. Inténtelo nuevamente.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )


class PersonalReportPaginationView(discord.ui.View):
    """Private pagination for deterministic report references."""

    def __init__(
        self,
        *,
        requester_id: int,
        result: PersonalReportSearchResult,
        query: PersonalReportQuery,
        display_name: str,
    ) -> None:
        super().__init__(timeout=180, disable_on_timeout=True)
        self.requester_id = requester_id
        self.result = result
        self.query = query
        self.display_name = display_name
        self.page = 0
        self._synchronize_buttons()

    @property
    def page_count(self) -> int:
        return max(1, ceil(len(self.result.reports) / REPORTS_PER_PAGE))

    @property
    def current_embed(self) -> discord.Embed:
        return build_personal_reports_embed(
            self.result,
            self.query,
            display_name=self.display_name,
            page=self.page,
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            "Esta consulta pertenece a otro miembro.",
            ephemeral=True,
        )
        return False

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


async def send_personal_report_result(
    interaction: discord.Interaction,
    view: PersonalReportPaginationView,
) -> None:
    """Publish an ephemeral result without passing Pycord an invalid null view."""

    kwargs: dict[str, object] = {
        "embed": view.current_embed,
        "ephemeral": True,
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    if view.page_count > 1:
        kwargs["view"] = view
    await interaction.followup.send(**kwargs)


def build_personal_reports_embed(
    result: PersonalReportSearchResult,
    query: PersonalReportQuery,
    *,
    display_name: str,
    page: int = 0,
) -> discord.Embed:
    """Render a page of direct Discord references for one member."""

    page_count = max(1, ceil(len(result.reports) / REPORTS_PER_PAGE))
    safe_page = min(max(page, 0), page_count - 1)
    start = safe_page * REPORTS_PER_PAGE
    page_reports = result.reports[start : start + REPORTS_PER_PAGE]
    channel_scope = "Todas las estaciones" if query.channel_id is None else f"<#{query.channel_id}>"
    embed = discord.Embed(
        title="📁 Mis reportes",
        description=(
            f"**Periodo:** {PERIOD_LABELS[query.period]}\n"
            f"**Estado:** {STATUS_FILTER_LABELS[query.status]}\n"
            f"**Estación:** {channel_scope}"
        ),
        color=discord.Color.gold(),
    )
    if not page_reports:
        embed.add_field(
            name="Sin resultados",
            value="No encontré publicaciones propias que coincidan con estos filtros.",
            inline=False,
        )
    for report in page_reports:
        icon, status_label = STATUS_PRESENTATION[report.status]
        links = f"[Abrir publicación]({report.post_url})"
        if report.thread_url:
            links += f" · [Abrir hilo]({report.thread_url})"
        embed.add_field(
            name=f"{icon} {report.title}"[:256],
            value=(
                f"**{status_label}** · <#{report.channel_id}> · "
                f"{report.created_at.strftime('%d/%m/%y')}\n{links}"
            ),
            inline=False,
        )
    footer = f"Página {safe_page + 1}/{page_count} · {len(result.reports)} resultado(s) · {display_name}"
    if result.truncated:
        footer += " · Búsqueda limitada; acote los filtros"
    embed.set_footer(text=footer)
    return embed


def build_personal_summary_embed(
    result: PersonalReportSearchResult,
    *,
    display_name: str,
    now: datetime | None = None,
) -> discord.Embed:
    """Render a private current-month snapshot for `/wv mi-resumen`."""

    reference_time = now or datetime.now(UTC)
    summary = PersonalReportSummary.from_reports(result.reports)
    period = f"{MONTH_NAMES[reference_time.month - 1].capitalize()} {reference_time.year}"
    embed = discord.Embed(
        title=f"⭐ Mi resumen · {period}",
        description=(
            f"📂 **Presentados:** {summary.total}\n"
            f"✅ **Aprobados:** {summary.approved}\n"
            f"❌ **Con correcciones:** {summary.corrections_required}\n"
            f"⏳ **Pendientes:** {summary.pending}\n"
            f"⚠️ **Requieren supervisión:** {summary.inconclusive}\n\n"
            f"**Aprobación actual:** {summary.approval_rate:.1f}%"
        ),
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="Navegación rápida",
        value="Use `/wv mis-reportes` para filtrar y abrir cada publicación o hilo.",
        inline=False,
    )
    footer = f"Estado actual según las reacciones de {display_name}"
    if result.truncated:
        footer += " · Consulta limitada por volumen"
    embed.set_footer(text=footer)
    return embed


def _can_read_history(channel: discord.TextChannel, member: discord.Member) -> bool:
    permissions = channel.permissions_for(member)
    return permissions.view_channel and permissions.read_message_history


def _status_from_bot_reactions(message: discord.Message) -> PersonalReportStatus:
    detected = {
        BOT_STATUS_REACTIONS[str(reaction.emoji)]
        for reaction in message.reactions
        if reaction.me and str(reaction.emoji) in BOT_STATUS_REACTIONS
    }
    for status in (
        PersonalReportStatus.APPROVED,
        PersonalReportStatus.CORRECTIONS_REQUIRED,
        PersonalReportStatus.INCONCLUSIVE,
        PersonalReportStatus.IN_REVIEW,
    ):
        if status in detected:
            return status
    return PersonalReportStatus.PENDING


def _is_report_candidate(
    message: discord.Message,
    member_id: int,
    status: PersonalReportStatus,
) -> bool:
    if message.author.id != member_id or message.author.bot or message.webhook_id is not None:
        return False
    if message.reference is not None:
        return False
    return bool(message.attachments) or status is not PersonalReportStatus.PENDING


def _to_personal_report(
    message: discord.Message,
    status: PersonalReportStatus,
    guild_id: int,
) -> PersonalReport:
    thread = message.thread
    thread_url = (
        f"https://discord.com/channels/{guild_id}/{thread.id}" if thread is not None else None
    )
    return PersonalReport(
        message_id=message.id,
        channel_id=message.channel.id,
        created_at=message.created_at,
        title=_report_title(message),
        status=status,
        post_url=message.jump_url,
        thread_url=thread_url,
    )


def _report_title(message: discord.Message) -> str:
    without_mentions = re.sub(r"<@!?\d+>", "", message.content)
    first_line = next((line.strip() for line in without_mentions.splitlines() if line.strip()), "")
    normalized = " ".join(first_line.split())
    if not normalized:
        normalized = f"Expediente #{str(message.id)[-6:]}"
    return discord.utils.escape_markdown(normalized)[:80]
