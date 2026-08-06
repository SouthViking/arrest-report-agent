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
from arrest_report_agent.config import (
    AppSettings,
    PersonaAccessMessages,
    PersonaFailureMessages,
    PersonaNavigationMessages,
    PersonaPersonalReportMessages,
)
from arrest_report_agent.config.logging import exception_fields, log_event

from .supervisor_access import SupervisorRolePolicy

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
        self.supervisor_policy = SupervisorRolePolicy.from_settings(settings)

    def visible_channels(
        self,
        guild: discord.Guild,
        member: discord.Member,
    ) -> tuple[discord.TextChannel, ...]:
        configured_ids = self.settings.report_channel_allowlist.ids
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
        return await self._find(
            guild,
            member,
            query,
            author_id=member.id,
            now=now,
        )

    async def find_all(
        self,
        guild: discord.Guild,
        viewer: discord.Member,
        query: PersonalReportQuery,
        *,
        now: datetime | None = None,
    ) -> PersonalReportSearchResult:
        """Return reports from every human author in channels visible to the viewer."""

        return await self._find(
            guild,
            viewer,
            query,
            author_id=None,
            now=now,
        )

    async def _find(
        self,
        guild: discord.Guild,
        viewer: discord.Member,
        query: PersonalReportQuery,
        *,
        author_id: int | None,
        now: datetime | None,
    ) -> PersonalReportSearchResult:
        reference_time = now or datetime.now(UTC)
        after = query.period.starts_at(reference_time)
        channels = self.visible_channels(guild, viewer)
        if query.channel_id is not None:
            channels = tuple(channel for channel in channels if channel.id == query.channel_id)

        reports: list[PersonalReport] = []
        supervisor_member_cache: dict[int, discord.Member | None] = {}
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
                    status = await _status_from_trusted_reactions(
                        message,
                        self.supervisor_policy,
                        supervisor_member_cache,
                    )
                    if not _is_report_candidate(message, author_id, status):
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
                    user_id=viewer.id,
                    **exception_fields(error),
                )
                continue
            if scanned >= MAX_HISTORY_MESSAGES_PER_CHANNEL:
                truncated = True

        reports.sort(key=lambda report: (report.created_at, report.message_id), reverse=True)
        return PersonalReportSearchResult(reports=tuple(reports), truncated=truncated)


class PersonalReportsModal(discord.ui.DesignerModal):
    """Single-user filter modal for `/mis-reportes`."""

    def __init__(
        self,
        *,
        finder: DiscordPersonalReportFinder,
        guild: discord.Guild,
        member: discord.Member,
        channels: tuple[discord.TextChannel, ...],
        display_name: str,
        access_messages: PersonaAccessMessages,
        failure_messages: PersonaFailureMessages,
        ui: PersonaPersonalReportMessages,
        navigation: PersonaNavigationMessages,
    ) -> None:
        self.finder = finder
        self.guild = guild
        self.member = member
        self.display_name = display_name
        self.access_messages = access_messages
        self.failure_messages = failure_messages
        self.ui = ui
        self.navigation = navigation
        period_labels = build_period_labels(navigation)
        status_labels = build_status_filter_labels(navigation)
        self.period_select = discord.ui.StringSelect(
            custom_id="personal_reports_period",
            options=[
                discord.SelectOption(
                    label=label,
                    value=period.value,
                    default=period is PersonalReportPeriod.CURRENT_MONTH,
                )
                for period, label in period_labels.items()
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
                for status, label in status_labels.items()
            ],
        )
        channel_options = [
            discord.SelectOption(
                label=navigation.all_stations,
                value="all",
                default=True,
            )
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
                ui.period_label,
                self.period_select,
                description=ui.period_description,
            ),
            discord.ui.Label(ui.status_label, self.status_select),
            discord.ui.Label(ui.station_label, self.channel_select),
            title=ui.modal_title,
            custom_id=f"personal_reports:{member.id}",
            timeout=180,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.member.id:
            await interaction.response.send_message(
                self.access_messages.other_member_query,
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
                messages=self.access_messages,
                ui=self.ui,
                navigation=self.navigation,
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
                self.failure_messages.personal_report_query_failure,
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
        messages: PersonaAccessMessages,
        ui: PersonaPersonalReportMessages,
        navigation: PersonaNavigationMessages,
    ) -> None:
        super().__init__(timeout=180, disable_on_timeout=True)
        self.requester_id = requester_id
        self.result = result
        self.query = query
        self.display_name = display_name
        self.messages = messages
        self.ui = ui
        self.navigation = navigation
        self.page = 0
        self.previous_page.label = navigation.previous_button
        self.next_page.label = navigation.next_button
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
            ui=self.ui,
            navigation=self.navigation,
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            self.messages.other_member_query,
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
    ui: PersonaPersonalReportMessages,
    navigation: PersonaNavigationMessages,
) -> discord.Embed:
    """Render a page of direct Discord references for one member."""

    page_count = max(1, ceil(len(result.reports) / REPORTS_PER_PAGE))
    safe_page = min(max(page, 0), page_count - 1)
    start = safe_page * REPORTS_PER_PAGE
    page_reports = result.reports[start : start + REPORTS_PER_PAGE]
    period_labels = build_period_labels(navigation)
    status_filter_labels = build_status_filter_labels(navigation)
    status_presentation = build_status_presentation(navigation)
    channel_scope = navigation.all_stations if query.channel_id is None else f"<#{query.channel_id}>"
    embed = discord.Embed(
        title=ui.reports_title,
        description=ui.reports_description.format(
            period=period_labels[query.period],
            status=status_filter_labels[query.status],
            station=channel_scope,
        ),
        color=discord.Color.gold(),
    )
    if not page_reports:
        embed.add_field(
            name=ui.empty_title,
            value=ui.empty_body,
            inline=False,
        )
    for report in page_reports:
        icon, status_label = status_presentation[report.status]
        links = f"[{navigation.open_post}]({report.post_url})"
        if report.thread_url:
            links += f" · [{navigation.open_thread}]({report.thread_url})"
        embed.add_field(
            name=f"{icon} {report.title}"[:256],
            value=(
                f"**{status_label}** · <#{report.channel_id}> · "
                f"{report.created_at.strftime('%d/%m/%y')}\n{links}"
            ),
            inline=False,
        )
    footer = ui.reports_footer.format(
        page=safe_page + 1,
        page_count=page_count,
        count=len(result.reports),
        display_name=display_name,
    )
    if result.truncated:
        footer += f" · {ui.reports_footer_truncated}"
    embed.set_footer(text=footer)
    return embed


def build_personal_summary_embed(
    result: PersonalReportSearchResult,
    *,
    display_name: str,
    now: datetime | None = None,
    ui: PersonaPersonalReportMessages,
) -> discord.Embed:
    """Render a private current-month snapshot for `/mi-resumen`."""

    reference_time = now or datetime.now(UTC)
    summary = PersonalReportSummary.from_reports(result.reports)
    period = f"{MONTH_NAMES[reference_time.month - 1].capitalize()} {reference_time.year}"
    embed = discord.Embed(
        title=ui.summary_title.format(period=period),
        description=ui.summary_description.format(
            total=summary.total,
            approved=summary.approved,
            corrections_required=summary.corrections_required,
            pending=summary.pending,
            inconclusive=summary.inconclusive,
            approval_rate=f"{summary.approval_rate:.1f}",
        ),
        color=discord.Color.gold(),
    )
    embed.add_field(
        name=ui.summary_navigation_title,
        value=ui.summary_navigation_body,
        inline=False,
    )
    footer = ui.summary_footer.format(display_name=display_name)
    if result.truncated:
        footer += f" · {ui.summary_footer_truncated}"
    embed.set_footer(text=footer)
    return embed


def build_period_labels(
    messages: PersonaNavigationMessages,
) -> dict[PersonalReportPeriod, str]:
    return {
        PersonalReportPeriod.CURRENT_MONTH: messages.period_current_month,
        PersonalReportPeriod.LAST_7_DAYS: messages.period_last_7_days,
        PersonalReportPeriod.LAST_30_DAYS: messages.period_last_30_days,
        PersonalReportPeriod.LAST_90_DAYS: messages.period_last_90_days,
    }


def build_status_filter_labels(
    messages: PersonaNavigationMessages,
) -> dict[PersonalReportStatusFilter, str]:
    return {
        PersonalReportStatusFilter.ALL: messages.status_filter_all,
        PersonalReportStatusFilter.PENDING: messages.status_filter_pending,
        PersonalReportStatusFilter.APPROVED: messages.status_filter_approved,
        PersonalReportStatusFilter.CORRECTIONS_REQUIRED: (
            messages.status_filter_corrections_required
        ),
        PersonalReportStatusFilter.INCONCLUSIVE: messages.status_filter_inconclusive,
    }


def build_status_presentation(
    messages: PersonaNavigationMessages,
) -> dict[PersonalReportStatus, tuple[str, str]]:
    return {
        PersonalReportStatus.PENDING: ("⏳", messages.status_pending),
        PersonalReportStatus.IN_REVIEW: ("🔎", messages.status_in_review),
        PersonalReportStatus.APPROVED: ("✅", messages.status_approved),
        PersonalReportStatus.CORRECTIONS_REQUIRED: (
            "❌",
            messages.status_corrections_required,
        ),
        PersonalReportStatus.INCONCLUSIVE: ("⚠️", messages.status_inconclusive),
    }


def _can_read_history(channel: discord.TextChannel, member: discord.Member) -> bool:
    permissions = channel.permissions_for(member)
    return permissions.view_channel and permissions.read_message_history


async def _status_from_trusted_reactions(
    message: discord.Message,
    supervisor_policy: SupervisorRolePolicy,
    member_cache: dict[int, discord.Member | None],
) -> PersonalReportStatus:
    detected: set[PersonalReportStatus] = set()
    for reaction in message.reactions:
        emoji = str(reaction.emoji)
        if emoji not in BOT_STATUS_REACTIONS:
            continue
        if reaction.me or await _has_authorized_supervisor_reaction(
            reaction,
            message,
            supervisor_policy,
            member_cache,
        ):
            detected.add(BOT_STATUS_REACTIONS[emoji])

    for status in (
        PersonalReportStatus.APPROVED,
        PersonalReportStatus.CORRECTIONS_REQUIRED,
        PersonalReportStatus.INCONCLUSIVE,
        PersonalReportStatus.IN_REVIEW,
    ):
        if status in detected:
            return status
    return PersonalReportStatus.PENDING


async def _has_authorized_supervisor_reaction(
    reaction: discord.Reaction,
    message: discord.Message,
    supervisor_policy: SupervisorRolePolicy,
    member_cache: dict[int, discord.Member | None],
) -> bool:
    try:
        async for reactor in reaction.users():
            if reactor.bot:
                continue
            if not supervisor_policy.is_restricted:
                return True
            member = await _resolve_reactor_member(
                message,
                reactor,
                member_cache,
            )
            if member is not None and supervisor_policy.allows(member):
                return True
    except (discord.Forbidden, discord.HTTPException) as error:
        guild_id = message.guild.id if message.guild is not None else "none"
        log_event(
            LOGGER,
            "warning",
            "report_status_reactors_unavailable",
            exc_info=error,
            guild_id=guild_id,
            channel_id=message.channel.id,
            message_id=message.id,
            emoji=str(reaction.emoji),
            **exception_fields(error),
        )
    return False


async def _resolve_reactor_member(
    message: discord.Message,
    reactor: discord.User | discord.Member,
    member_cache: dict[int, discord.Member | None],
) -> discord.Member | None:
    if isinstance(reactor, discord.Member):
        member_cache[reactor.id] = reactor
        return reactor
    if message.guild is None:
        return None
    if reactor.id in member_cache:
        return member_cache[reactor.id]

    member = message.guild.get_member(reactor.id)
    if member is None:
        try:
            member = await message.guild.fetch_member(reactor.id)
        except discord.NotFound:
            member_cache[reactor.id] = None
            return None
        except (discord.Forbidden, discord.HTTPException) as error:
            log_event(
                LOGGER,
                "warning",
                "report_status_reactor_unavailable",
                exc_info=error,
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                message_id=message.id,
                reactor_id=reactor.id,
                **exception_fields(error),
            )
            return None

    member_cache[reactor.id] = member
    return member


def _is_report_candidate(
    message: discord.Message,
    member_id: int | None,
    status: PersonalReportStatus,
) -> bool:
    if member_id is not None and message.author.id != member_id:
        return False
    if message.author.bot or message.webhook_id is not None:
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
        author_id=message.author.id,
        author_display_name=_author_display_name(message.author),
    )


def _report_title(message: discord.Message) -> str:
    without_mentions = re.sub(r"<@!?\d+>", "", message.content)
    first_line = next((line.strip() for line in without_mentions.splitlines() if line.strip()), "")
    normalized = " ".join(first_line.split())
    if not normalized:
        normalized = f"Expediente #{str(message.id)[-6:]}"
    return discord.utils.escape_markdown(normalized)[:80]


def _author_display_name(author: discord.User | discord.Member) -> str:
    display_name = getattr(author, "display_name", None)
    if not display_name:
        display_name = getattr(author, "global_name", None) or getattr(author, "name", None)
    return str(display_name or f"Usuario {author.id}")
