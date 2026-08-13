"""Discord modal and rendering for deterministic report-error analytics."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from arrest_report_agent.application import PersonalReportPeriod
from arrest_report_agent.config import (
    PersonaAccessMessages,
    PersonaFailureMessages,
    PersonaNavigationMessages,
    PersonaSupervisionMessages,
)
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.domain import FindingSeverity, ReviewIssueType

from .models import ReportErrorAnalyticsResult
from .repository import ReportReviewRepository

if TYPE_CHECKING:
    from arrest_report_agent.infrastructure.discord.supervisor_access import SupervisorRolePolicy

LOGGER = logging.getLogger(__name__)
VIEW_TIMEOUT_SECONDS = 180


def _build_period_labels(
    messages: PersonaNavigationMessages,
) -> dict[PersonalReportPeriod, str]:
    """Build analytics period labels without importing the aggregate Discord adapter."""

    return {
        PersonalReportPeriod.CURRENT_MONTH: messages.period_current_month,
        PersonalReportPeriod.LAST_7_DAYS: messages.period_last_7_days,
        PersonalReportPeriod.LAST_30_DAYS: messages.period_last_30_days,
        PersonalReportPeriod.LAST_90_DAYS: messages.period_last_90_days,
    }


class ReportErrorsModal(discord.ui.DesignerModal):
    """Collect bounded analytics filters without inline slash arguments."""

    def __init__(
        self,
        *,
        repository: ReportReviewRepository,
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
        self.repository = repository
        self.policy = policy
        self.guild = guild
        self.member = member
        self.display_name = display_name
        self.access_messages = access_messages
        self.failure_messages = failure_messages
        self.ui = ui
        self.navigation = navigation
        period_labels = _build_period_labels(navigation)
        self.period_select = discord.ui.StringSelect(
            custom_id="report_errors_period",
            options=[
                discord.SelectOption(
                    label=label,
                    value=period.value,
                    default=period is PersonalReportPeriod.CURRENT_MONTH,
                )
                for period, label in period_labels.items()
            ],
        )
        self.channel_select = discord.ui.StringSelect(
            custom_id="report_errors_channel",
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
        self.severity_select = discord.ui.StringSelect(
            custom_id="report_errors_severity",
            options=[
                discord.SelectOption(
                    label=ui.report_errors_severity_all,
                    value="all",
                    default=True,
                ),
                discord.SelectOption(
                    label=ui.report_errors_severity_required,
                    value=FindingSeverity.REQUIRED.value,
                    emoji="🔴",
                ),
                discord.SelectOption(
                    label=ui.report_errors_severity_warning,
                    value=FindingSeverity.WARNING.value,
                    emoji="🟠",
                ),
                discord.SelectOption(
                    label=ui.report_errors_severity_observation,
                    value=FindingSeverity.OBSERVATION.value,
                    emoji="🔎",
                ),
            ],
        )
        self.limit_select = discord.ui.StringSelect(
            custom_id="report_errors_limit",
            options=[
                discord.SelectOption(label="5", value="5"),
                discord.SelectOption(label="10", value="10", default=True),
                discord.SelectOption(label="Todas", value=str(len(ReviewIssueType))),
            ],
        )
        super().__init__(
            discord.ui.Label(ui.period_label, self.period_select),
            discord.ui.Label(ui.station_label, self.channel_select),
            discord.ui.Label(ui.report_errors_severity_label, self.severity_select),
            discord.ui.Label(ui.report_errors_limit_label, self.limit_select),
            title=ui.report_errors_modal_title,
            custom_id=f"report_errors:{member.id}",
            timeout=VIEW_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.member.id or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                self.access_messages.other_supervisor_query,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not self.policy.allows(interaction.user):
            await interaction.response.send_message(
                self.access_messages.supervisor_unauthorized,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        period = PersonalReportPeriod(self.period_select.values[0])
        raw_channel_id = self.channel_select.values[0]
        raw_severity = self.severity_select.values[0]
        channel_id = None if raw_channel_id == "all" else int(raw_channel_id)
        severity = None if raw_severity == "all" else FindingSeverity(raw_severity)
        limit = int(self.limit_select.values[0])
        await interaction.response.defer(ephemeral=False)
        try:
            result = await self.repository.aggregate_errors(
                guild_id=self.guild.id,
                period=period,
                channel_id=channel_id,
                severity=severity,
                limit=limit,
            )
            await interaction.followup.send(
                embed=build_report_errors_embed(
                    result,
                    period=period,
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
                "report_error_analytics_completed",
                guild_id=self.guild.id,
                user_id=self.member.id,
                period=period.value,
                channel_id=channel_id or "all",
                severity=severity.value if severity is not None else "all",
                total_reports=result.total_reports,
                total_attempts=result.total_attempts,
                result_count=len(result.entries),
            )
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "report_error_analytics_failed",
                exc_info=error,
                guild_id=self.guild.id,
                user_id=self.member.id,
                interaction_id=interaction.id,
                **exception_fields(error),
            )
            await interaction.followup.send(
                self.failure_messages.supervision_query_failure,
                ephemeral=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )


def build_report_errors_embed(
    result: ReportErrorAnalyticsResult,
    *,
    period: PersonalReportPeriod,
    display_name: str,
    ui: PersonaSupervisionMessages,
    navigation: PersonaNavigationMessages,
) -> discord.Embed:
    severity_labels = {
        None: ui.report_errors_severity_all,
        FindingSeverity.REQUIRED: ui.report_errors_severity_required,
        FindingSeverity.WARNING: ui.report_errors_severity_warning,
        FindingSeverity.OBSERVATION: ui.report_errors_severity_observation,
    }
    station = navigation.all_stations if result.channel_id is None else f"<#{result.channel_id}>"
    embed = discord.Embed(
        title=ui.report_errors_title,
        description=ui.report_errors_description.format(
            period=_build_period_labels(navigation)[period],
            station=station,
            severity=severity_labels[result.severity],
            reports=result.total_reports,
            attempts=result.total_attempts,
        ),
        color=discord.Color.blurple(),
    )
    if not result.entries:
        embed.add_field(
            name="Sin hallazgos",
            value=ui.report_errors_empty_body,
            inline=False,
        )
    for position, entry in enumerate(result.entries, start=1):
        embed.add_field(
            name=f"{position}. {entry.display_name} — {entry.report_count} reporte(s)"[:256],
            value=(
                f"🔴 {entry.required_reports} · 🟠 {entry.warning_reports} · "
                f"🔎 {entry.observation_reports} · "
                f"{entry.finding_count} hallazgo(s)"
            ),
            inline=False,
        )
    embed.set_footer(
        text=ui.report_errors_footer.format(
            shown=len(result.entries),
            total=result.total_issue_types,
            display_name=display_name,
        )
    )
    return embed
