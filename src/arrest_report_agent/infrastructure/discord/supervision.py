"""Role-gated deterministic supervision commands for Discord report snapshots."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from math import ceil

import discord

from arrest_report_agent.application import (
    PersonalReportPeriod,
    PersonalReportQuery,
    PersonalReportSearchResult,
    PersonalReportStatusFilter,
    SupervisorRankingEntry,
    SupervisorRankingMetric,
    build_approved_report_ranking,
    build_open_report_ranking,
    build_supervisor_ranking,
)
from arrest_report_agent.config import (
    PersonaAccessMessages,
    PersonaFailureMessages,
    PersonaNavigationMessages,
    PersonaSupervisionMessages,
)
from arrest_report_agent.config.logging import exception_fields, log_event

from .personal_reports import (
    DiscordPersonalReportFinder,
    build_period_labels,
    build_status_presentation,
)
from .supervisor_access import SupervisorRolePolicy

LOGGER = logging.getLogger(__name__)

SUPERVISION_VIEW_TIMEOUT_SECONDS = 180
PENDING_REPORTS_PER_PAGE = 5
RANKING_ENTRIES_PER_PAGE = 10
SHAME_RANKING_LIMIT = 10
HONOR_RANKING_LIMIT = 10


@dataclass(frozen=True, slots=True)
class ShameRankingSection:
    """Open-report ranking for one configured Discord report channel."""

    channel_id: int
    channel_name: str
    ranking: tuple[SupervisorRankingEntry, ...]


@dataclass(frozen=True, slots=True)
class HonorRankingSection:
    """Approved-report ranking for one configured Discord report channel."""

    channel_id: int
    channel_name: str
    ranking: tuple[SupervisorRankingEntry, ...]


def build_supervisor_unauthorized_message(messages: PersonaAccessMessages) -> str:
    return messages.supervisor_unauthorized


class SupervisorRequesterView(discord.ui.View):
    def __init__(self, requester_id: int, messages: PersonaAccessMessages) -> None:
        super().__init__(timeout=SUPERVISION_VIEW_TIMEOUT_SECONDS, disable_on_timeout=True)
        self.requester_id = requester_id
        self.messages = messages

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            self.messages.other_supervisor_query,
            ephemeral=True,
        )
        return False


class PendingReportsModal(discord.ui.DesignerModal):
    def __init__(
        self,
        *,
        finder: DiscordPersonalReportFinder,
        policy: SupervisorRolePolicy,
        guild: discord.Guild,
        member: discord.Member,
        channels: tuple[discord.TextChannel, ...],
        display_name: str,
        access_messages: PersonaAccessMessages,
        failure_messages: PersonaFailureMessages,
        ui: PersonaSupervisionMessages,
        navigation: PersonaNavigationMessages,
    ) -> None:
        self.finder = finder
        self.policy = policy
        self.guild = guild
        self.member = member
        self.display_name = display_name
        self.access_messages = access_messages
        self.failure_messages = failure_messages
        self.ui = ui
        self.navigation = navigation
        self.period_select = _period_select("supervision_pending_period", navigation)
        self.channel_select = _channel_select(
            "supervision_pending_channel",
            channels,
            navigation,
        )
        super().__init__(
            discord.ui.Label(ui.period_label, self.period_select),
            discord.ui.Label(ui.station_label, self.channel_select),
            title=ui.pending_modal_title,
            custom_id=f"supervision_pending:{member.id}",
            timeout=SUPERVISION_VIEW_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _authorize_modal_interaction(
            interaction,
            requester_id=self.member.id,
            policy=self.policy,
            messages=self.access_messages,
        ):
            return
        period = PersonalReportPeriod(self.period_select.values[0])
        raw_channel_id = self.channel_select.values[0]
        query = PersonalReportQuery(
            period=period,
            status=PersonalReportStatusFilter.PENDING,
            channel_id=None if raw_channel_id == "all" else int(raw_channel_id),
        )
        await interaction.response.defer(ephemeral=True)
        try:
            result = await self.finder.find_all(self.guild, self.member, query)
            view = PendingReportsPaginationView(
                requester_id=self.member.id,
                result=result,
                query=query,
                display_name=self.display_name,
                messages=self.access_messages,
                ui=self.ui,
                navigation=self.navigation,
            )
            await _send_optional_view(interaction, embed=view.current_embed, view=view)
            log_event(
                LOGGER,
                "info",
                "supervisor_pending_query_completed",
                guild_id=self.guild.id,
                user_id=self.member.id,
                period=period.value,
                channel_id=query.channel_id or "all",
                result_count=len(result.reports),
                truncated=result.truncated,
            )
        except Exception as error:
            await _publish_supervision_failure(
                interaction,
                error,
                event="supervisor_pending_query_failed",
                guild_id=self.guild.id,
                user_id=self.member.id,
                messages=self.failure_messages,
            )


class RankingModal(discord.ui.DesignerModal):
    def __init__(
        self,
        *,
        finder: DiscordPersonalReportFinder,
        policy: SupervisorRolePolicy,
        guild: discord.Guild,
        member: discord.Member,
        channels: tuple[discord.TextChannel, ...],
        display_name: str,
        access_messages: PersonaAccessMessages,
        failure_messages: PersonaFailureMessages,
        ui: PersonaSupervisionMessages,
        navigation: PersonaNavigationMessages,
    ) -> None:
        self.finder = finder
        self.policy = policy
        self.guild = guild
        self.member = member
        self.display_name = display_name
        self.access_messages = access_messages
        self.failure_messages = failure_messages
        self.ui = ui
        self.navigation = navigation
        self.period_select = _period_select("supervision_ranking_period", navigation)
        metric_labels = build_ranking_metric_labels(ui)
        self.metric_select = discord.ui.StringSelect(
            custom_id="supervision_ranking_metric",
            options=[
                discord.SelectOption(
                    label=label,
                    value=metric.value,
                    default=metric is SupervisorRankingMetric.APPROVED,
                )
                for metric, label in metric_labels.items()
            ],
        )
        self.channel_select = _channel_select(
            "supervision_ranking_channel",
            channels,
            navigation,
        )
        super().__init__(
            discord.ui.Label(ui.period_label, self.period_select),
            discord.ui.Label(ui.ranking_order_label, self.metric_select),
            discord.ui.Label(ui.station_label, self.channel_select),
            title=ui.ranking_modal_title,
            custom_id=f"supervision_ranking:{member.id}",
            timeout=SUPERVISION_VIEW_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _authorize_modal_interaction(
            interaction,
            requester_id=self.member.id,
            policy=self.policy,
            messages=self.access_messages,
        ):
            return
        period = PersonalReportPeriod(self.period_select.values[0])
        metric = SupervisorRankingMetric(self.metric_select.values[0])
        raw_channel_id = self.channel_select.values[0]
        query = PersonalReportQuery(
            period=period,
            status=PersonalReportStatusFilter.ALL,
            channel_id=None if raw_channel_id == "all" else int(raw_channel_id),
        )
        await interaction.response.defer(ephemeral=True)
        try:
            result = await self.finder.find_all(self.guild, self.member, query)
            ranking = build_supervisor_ranking(result.reports, metric)
            view = RankingPaginationView(
                requester_id=self.member.id,
                ranking=ranking,
                metric=metric,
                query=query,
                truncated=result.truncated,
                display_name=self.display_name,
                messages=self.access_messages,
                ui=self.ui,
                navigation=self.navigation,
            )
            await _send_optional_view(interaction, embed=view.current_embed, view=view)
            log_event(
                LOGGER,
                "info",
                "supervisor_ranking_query_completed",
                guild_id=self.guild.id,
                user_id=self.member.id,
                period=period.value,
                metric=metric.value,
                channel_id=query.channel_id or "all",
                report_count=len(result.reports),
                member_count=len(ranking),
                truncated=result.truncated,
            )
        except Exception as error:
            await _publish_supervision_failure(
                interaction,
                error,
                event="supervisor_ranking_query_failed",
                guild_id=self.guild.id,
                user_id=self.member.id,
                messages=self.failure_messages,
            )


class ShameRankingModal(discord.ui.DesignerModal):
    """Public period selector for the playful open-report ranking."""

    def __init__(
        self,
        *,
        finder: DiscordPersonalReportFinder,
        guild: discord.Guild,
        member: discord.Member,
        display_name: str,
        access_messages: PersonaAccessMessages,
        failure_messages: PersonaFailureMessages,
        ui: PersonaSupervisionMessages,
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
        self.period_select = _period_select("shame_ranking_period", navigation)
        super().__init__(
            discord.ui.Label(ui.period_label, self.period_select),
            title=ui.shame_modal_title,
            custom_id=f"shame_ranking:{member.id}",
            timeout=SUPERVISION_VIEW_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.member.id:
            await interaction.response.send_message(
                self.access_messages.other_member_query,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        period = PersonalReportPeriod(self.period_select.values[0])
        query = PersonalReportQuery(
            period=period,
            status=PersonalReportStatusFilter.ALL,
        )
        await interaction.response.defer(ephemeral=False)
        try:
            result = await self.finder.find_all(self.guild, self.member, query)
            channels = self.finder.visible_channels(self.guild, self.member)
            sections = tuple(
                ShameRankingSection(
                    channel_id=channel.id,
                    channel_name=channel.name,
                    ranking=build_open_report_ranking(
                        tuple(report for report in result.reports if report.channel_id == channel.id)
                    ),
                )
                for channel in channels
            )
            await interaction.followup.send(
                embed=build_shame_ranking_embed(
                    sections,
                    period=period,
                    truncated=result.truncated,
                    display_name=self.display_name,
                    ui=self.ui,
                    navigation=self.navigation,
                ),
                ephemeral=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log_event(
                LOGGER,
                "info",
                "open_report_shame_completed",
                guild_id=self.guild.id,
                user_id=self.member.id,
                period=period.value,
                report_count=len(result.reports),
                channel_count=len(sections),
                channel_ids=[section.channel_id for section in sections],
                member_count=len({entry.author_id for section in sections for entry in section.ranking}),
                truncated=result.truncated,
            )
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "open_report_shame_failed",
                exc_info=error,
                guild_id=self.guild.id,
                user_id=self.member.id,
                interaction_id=interaction.id,
                **exception_fields(error),
            )
            await interaction.followup.send(
                self.failure_messages.shame_query_failure,
                ephemeral=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )


class HonorRankingModal(discord.ui.DesignerModal):
    """Public period selector for the approved-report honor ranking."""

    def __init__(
        self,
        *,
        finder: DiscordPersonalReportFinder,
        guild: discord.Guild,
        member: discord.Member,
        display_name: str,
        access_messages: PersonaAccessMessages,
        failure_messages: PersonaFailureMessages,
        ui: PersonaSupervisionMessages,
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
        self.period_select = _period_select("honor_ranking_period", navigation)
        super().__init__(
            discord.ui.Label(ui.period_label, self.period_select),
            title=ui.honor_modal_title,
            custom_id=f"honor_ranking:{member.id}",
            timeout=SUPERVISION_VIEW_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.member.id:
            await interaction.response.send_message(
                self.access_messages.other_member_query,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        period = PersonalReportPeriod(self.period_select.values[0])
        query = PersonalReportQuery(
            period=period,
            status=PersonalReportStatusFilter.ALL,
        )
        await interaction.response.defer(ephemeral=False)
        try:
            result = await self.finder.find_all(self.guild, self.member, query)
            channels = self.finder.visible_channels(self.guild, self.member)
            sections = tuple(
                HonorRankingSection(
                    channel_id=channel.id,
                    channel_name=channel.name,
                    ranking=build_approved_report_ranking(
                        tuple(report for report in result.reports if report.channel_id == channel.id)
                    ),
                )
                for channel in channels
            )
            await interaction.followup.send(
                embed=build_honor_ranking_embed(
                    sections,
                    period=period,
                    truncated=result.truncated,
                    display_name=self.display_name,
                    ui=self.ui,
                    navigation=self.navigation,
                ),
                ephemeral=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log_event(
                LOGGER,
                "info",
                "approved_report_honor_completed",
                guild_id=self.guild.id,
                user_id=self.member.id,
                period=period.value,
                report_count=len(result.reports),
                channel_count=len(sections),
                channel_ids=[section.channel_id for section in sections],
                member_count=len({entry.author_id for section in sections for entry in section.ranking}),
                truncated=result.truncated,
            )
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "approved_report_honor_failed",
                exc_info=error,
                guild_id=self.guild.id,
                user_id=self.member.id,
                interaction_id=interaction.id,
                **exception_fields(error),
            )
            await interaction.followup.send(
                self.failure_messages.honor_query_failure,
                ephemeral=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )


class PendingReportsPaginationView(SupervisorRequesterView):
    def __init__(
        self,
        *,
        requester_id: int,
        result: PersonalReportSearchResult,
        query: PersonalReportQuery,
        display_name: str,
        messages: PersonaAccessMessages,
        ui: PersonaSupervisionMessages,
        navigation: PersonaNavigationMessages,
    ) -> None:
        super().__init__(requester_id, messages)
        self.result = result
        self.query = query
        self.display_name = display_name
        self.ui = ui
        self.navigation = navigation
        self.page = 0
        self.previous_page.label = navigation.previous_button
        self.next_page.label = navigation.next_button
        self._synchronize_buttons()

    @property
    def page_count(self) -> int:
        return max(1, ceil(len(self.result.reports) / PENDING_REPORTS_PER_PAGE))

    @property
    def current_embed(self) -> discord.Embed:
        return build_pending_reports_embed(
            self.result,
            self.query,
            display_name=self.display_name,
            page=self.page,
            ui=self.ui,
            navigation=self.navigation,
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


class RankingPaginationView(SupervisorRequesterView):
    def __init__(
        self,
        *,
        requester_id: int,
        ranking: tuple[SupervisorRankingEntry, ...],
        metric: SupervisorRankingMetric,
        query: PersonalReportQuery,
        truncated: bool,
        display_name: str,
        messages: PersonaAccessMessages,
        ui: PersonaSupervisionMessages,
        navigation: PersonaNavigationMessages,
    ) -> None:
        super().__init__(requester_id, messages)
        self.ranking = ranking
        self.metric = metric
        self.query = query
        self.truncated = truncated
        self.display_name = display_name
        self.ui = ui
        self.navigation = navigation
        self.page = 0
        self.previous_page.label = navigation.previous_button
        self.next_page.label = navigation.next_button
        self._synchronize_buttons()

    @property
    def page_count(self) -> int:
        return max(1, ceil(len(self.ranking) / RANKING_ENTRIES_PER_PAGE))

    @property
    def current_embed(self) -> discord.Embed:
        return build_ranking_embed(
            self.ranking,
            metric=self.metric,
            query=self.query,
            truncated=self.truncated,
            display_name=self.display_name,
            page=self.page,
            ui=self.ui,
            navigation=self.navigation,
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


def build_pending_reports_embed(
    result: PersonalReportSearchResult,
    query: PersonalReportQuery,
    *,
    display_name: str,
    page: int = 0,
    ui: PersonaSupervisionMessages,
    navigation: PersonaNavigationMessages,
) -> discord.Embed:
    page_count = max(1, ceil(len(result.reports) / PENDING_REPORTS_PER_PAGE))
    safe_page = min(max(page, 0), page_count - 1)
    start = safe_page * PENDING_REPORTS_PER_PAGE
    reports = result.reports[start : start + PENDING_REPORTS_PER_PAGE]
    period_labels = build_period_labels(navigation)
    status_presentation = build_status_presentation(navigation)
    channel_scope = navigation.all_stations if query.channel_id is None else f"<#{query.channel_id}>"
    embed = discord.Embed(
        title=ui.pending_title,
        description=ui.pending_description.format(
            period=period_labels[query.period],
            station=channel_scope,
        ),
        color=discord.Color.gold(),
    )
    if not reports:
        embed.add_field(
            name=ui.pending_empty_title,
            value=ui.pending_empty_body,
            inline=False,
        )
    for report in reports:
        icon, status_label = status_presentation[report.status]
        author = f"<@{report.author_id}>" if report.author_id is not None else navigation.unknown_author
        links = f"[{navigation.open_post}]({report.post_url})"
        if report.thread_url:
            links += f" · [{navigation.open_thread}]({report.thread_url})"
        embed.add_field(
            name=f"{icon} {report.title}"[:256],
            value=(
                f"**{status_label}** · {author} · <#{report.channel_id}> · "
                f"{report.created_at.strftime('%d/%m/%y')}\n{links}"
            ),
            inline=False,
        )
    footer = ui.pending_footer.format(
        page=safe_page + 1,
        page_count=page_count,
        count=len(result.reports),
        display_name=display_name,
    )
    if result.truncated:
        footer += f" · {ui.pending_footer_truncated}"
    embed.set_footer(text=footer)
    return embed


def build_ranking_embed(
    ranking: tuple[SupervisorRankingEntry, ...],
    *,
    metric: SupervisorRankingMetric,
    query: PersonalReportQuery,
    truncated: bool,
    display_name: str,
    page: int = 0,
    ui: PersonaSupervisionMessages,
    navigation: PersonaNavigationMessages,
) -> discord.Embed:
    page_count = max(1, ceil(len(ranking) / RANKING_ENTRIES_PER_PAGE))
    safe_page = min(max(page, 0), page_count - 1)
    start = safe_page * RANKING_ENTRIES_PER_PAGE
    entries = ranking[start : start + RANKING_ENTRIES_PER_PAGE]
    period_labels = build_period_labels(navigation)
    metric_labels = build_ranking_metric_labels(ui)
    channel_scope = navigation.all_stations if query.channel_id is None else f"<#{query.channel_id}>"
    lines = []
    for offset, entry in enumerate(entries):
        position = start + offset + 1
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(position, f"**{position}.**")
        lines.append(
            f"{medal} <@{entry.author_id}> — **{_ranking_value(entry, metric, ui)}**\n"
            f"↳ 📂 {entry.total} · ✅ {entry.approved} · ❌ {entry.corrections_required} · "
            f"📭 {entry.unreviewed} · ⏳ {entry.in_review} · "
            f"⚠️ {entry.inconclusive} · {entry.approval_rate:.1f}%"
        )
    embed = discord.Embed(
        title=ui.ranking_title,
        description=ui.ranking_description.format(
            metric=metric_labels[metric],
            period=period_labels[query.period],
            station=channel_scope,
            entries="\n".join(lines) if lines else ui.ranking_empty_body,
        ),
        color=discord.Color.gold(),
    )
    footer = ui.ranking_footer.format(
        page=safe_page + 1,
        page_count=page_count,
        count=len(ranking),
        display_name=display_name,
    )
    if truncated:
        footer += f" · {ui.ranking_footer_truncated}"
    embed.set_footer(text=footer)
    return embed


def build_shame_ranking_embed(
    sections: tuple[ShameRankingSection, ...],
    *,
    period: PersonalReportPeriod,
    truncated: bool,
    display_name: str,
    ui: PersonaSupervisionMessages,
    navigation: PersonaNavigationMessages,
) -> discord.Embed:
    """Render the public top of authors whose reports have not reached approval."""

    visible_author_ids: set[int] = set()
    all_author_ids: set[int] = set()
    embed = discord.Embed(
        title=ui.shame_title,
        description=(
            ui.shame_description.format(
                period=build_period_labels(navigation)[period],
                entries="",
            ).strip()
            + f"\n\n{ui.shame_legend}"
        ),
        color=discord.Color.red(),
    )

    for section in sections:
        entries = section.ranking[:SHAME_RANKING_LIMIT]
        all_author_ids.update(entry.author_id for entry in section.ranking)
        visible_author_ids.update(entry.author_id for entry in entries)
        lines: list[str] = []
        for position, entry in enumerate(entries, start=1):
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(position, f"**{position}.**")
            lines.append(
                f"{medal} <@{entry.author_id}> — "
                f"**{ui.shame_value.format(count=entry.total)}**\n"
                f"↳ 📭 {entry.unreviewed} · ⏳ {entry.in_review} · "
                f"❌ {entry.corrections_required} · "
                f"⚠️ {entry.inconclusive}"
            )
        embed.add_field(
            name=f"📍 #{section.channel_name}"[:256],
            value="\n".join(lines) if lines else ui.shame_empty_body,
            inline=False,
        )

    footer = ui.shame_footer.format(
        shown=len(visible_author_ids),
        count=len(all_author_ids),
        display_name=display_name,
    )
    if truncated:
        footer += f" · {ui.shame_footer_truncated}"
    embed.set_footer(text=footer)
    return embed


def build_honor_ranking_embed(
    sections: tuple[HonorRankingSection, ...],
    *,
    period: PersonalReportPeriod,
    truncated: bool,
    display_name: str,
    ui: PersonaSupervisionMessages,
    navigation: PersonaNavigationMessages,
) -> discord.Embed:
    """Render the public top of authors with currently approved reports."""

    visible_author_ids: set[int] = set()
    all_author_ids: set[int] = set()
    embed = discord.Embed(
        title=ui.honor_title,
        description=ui.honor_description.format(
            period=build_period_labels(navigation)[period],
        ),
        color=discord.Color.gold(),
    )

    for section in sections:
        entries = section.ranking[:HONOR_RANKING_LIMIT]
        all_author_ids.update(entry.author_id for entry in section.ranking)
        visible_author_ids.update(entry.author_id for entry in entries)
        lines: list[str] = []
        for position, entry in enumerate(entries, start=1):
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(position, f"**{position}.**")
            value = ui.honor_value.format(
                approved=entry.approved,
                total=entry.total,
                rate=f"{entry.approval_rate:.1f}",
            )
            lines.append(f"{medal} <@{entry.author_id}> — **{value}**")
        embed.add_field(
            name=f"📍 #{section.channel_name}"[:256],
            value="\n".join(lines) if lines else ui.honor_empty_body,
            inline=False,
        )

    footer = ui.honor_footer.format(
        shown=len(visible_author_ids),
        count=len(all_author_ids),
        display_name=display_name,
    )
    if truncated:
        footer += f" · {ui.honor_footer_truncated}"
    embed.set_footer(text=footer)
    return embed


async def _authorize_modal_interaction(
    interaction: discord.Interaction,
    *,
    requester_id: int,
    policy: SupervisorRolePolicy,
    messages: PersonaAccessMessages,
) -> bool:
    if (
        interaction.user.id == requester_id
        and isinstance(interaction.user, discord.Member)
        and policy.allows(interaction.user)
    ):
        return True
    await interaction.response.send_message(
        build_supervisor_unauthorized_message(messages),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    return False


def _period_select(
    custom_id: str,
    navigation: PersonaNavigationMessages,
) -> discord.ui.Select:
    period_labels = build_period_labels(navigation)
    return discord.ui.StringSelect(
        custom_id=custom_id,
        options=[
            discord.SelectOption(
                label=label,
                value=period.value,
                default=period is PersonalReportPeriod.CURRENT_MONTH,
            )
            for period, label in period_labels.items()
        ],
    )


def _channel_select(
    custom_id: str,
    channels: tuple[discord.TextChannel, ...],
    navigation: PersonaNavigationMessages,
) -> discord.ui.Select:
    return discord.ui.StringSelect(
        custom_id=custom_id,
        options=[
            discord.SelectOption(
                label=navigation.all_stations,
                value="all",
                default=True,
            ),
            *[
                discord.SelectOption(label=f"#{channel.name}"[:100], value=str(channel.id))
                for channel in channels[:24]
            ],
        ],
    )


async def _send_optional_view(
    interaction: discord.Interaction,
    *,
    embed: discord.Embed,
    view: PendingReportsPaginationView | RankingPaginationView,
) -> None:
    kwargs: dict[str, object] = {
        "embed": embed,
        "ephemeral": True,
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    if view.page_count > 1:
        kwargs["view"] = view
    await interaction.followup.send(**kwargs)


async def _publish_supervision_failure(
    interaction: discord.Interaction,
    error: Exception,
    *,
    event: str,
    guild_id: int,
    user_id: int,
    messages: PersonaFailureMessages,
) -> None:
    log_event(
        LOGGER,
        "error",
        event,
        exc_info=error,
        guild_id=guild_id,
        user_id=user_id,
        interaction_id=interaction.id,
        **exception_fields(error),
    )
    await interaction.followup.send(
        messages.supervision_query_failure,
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


def _ranking_value(
    entry: SupervisorRankingEntry,
    metric: SupervisorRankingMetric,
    ui: PersonaSupervisionMessages,
) -> str:
    if metric is SupervisorRankingMetric.APPROVED:
        return ui.ranking_value_approved.format(count=entry.approved)
    if metric is SupervisorRankingMetric.CORRECTIONS_REQUIRED:
        return ui.ranking_value_corrections_required.format(count=entry.corrections_required)
    if metric is SupervisorRankingMetric.APPROVAL_RATE:
        return ui.ranking_value_approval_rate.format(rate=f"{entry.approval_rate:.1f}")
    return ui.ranking_value_submitted.format(count=entry.total)


def build_ranking_metric_labels(
    messages: PersonaSupervisionMessages,
) -> dict[SupervisorRankingMetric, str]:
    return {
        SupervisorRankingMetric.SUBMITTED: messages.metric_submitted,
        SupervisorRankingMetric.APPROVED: messages.metric_approved,
        SupervisorRankingMetric.CORRECTIONS_REQUIRED: (messages.metric_corrections_required),
        SupervisorRankingMetric.APPROVAL_RATE: messages.metric_approval_rate,
    }
