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
    build_supervisor_ranking,
)
from arrest_report_agent.config import AppSettings, SnowflakeAllowlist
from arrest_report_agent.config.logging import exception_fields, log_event

from .personal_reports import (
    PERIOD_LABELS,
    STATUS_PRESENTATION,
    DiscordPersonalReportFinder,
)

LOGGER = logging.getLogger(__name__)

SUPERVISION_VIEW_TIMEOUT_SECONDS = 180
PENDING_REPORTS_PER_PAGE = 5
RANKING_ENTRIES_PER_PAGE = 10
RANKING_METRIC_LABELS = {
    SupervisorRankingMetric.SUBMITTED: "Reportes presentados",
    SupervisorRankingMetric.APPROVED: "Reportes aprobados",
    SupervisorRankingMetric.CORRECTIONS_REQUIRED: "Reportes con correcciones",
    SupervisorRankingMetric.APPROVAL_RATE: "Porcentaje de aprobación",
}


@dataclass(frozen=True, slots=True)
class SupervisorRolePolicy:
    """Central role policy that can later be backed by persisted configuration."""

    roles: SnowflakeAllowlist

    @classmethod
    def from_settings(cls, settings: AppSettings) -> SupervisorRolePolicy:
        return cls(roles=settings.supervisor_role_allowlist)

    @property
    def is_restricted(self) -> bool:
        return self.roles.is_restricted

    def allows(self, member: discord.Member) -> bool:
        if not self.roles.is_restricted:
            return True
        return any(self.roles.allows(role.id) for role in member.roles)


def build_supervisor_unauthorized_message() -> str:
    return (
        "🔒 **Archivo de supervisión restringido.** "
        "Su rol no figura entre los autorizados para esta consulta."
    )


class SupervisorRequesterView(discord.ui.View):
    def __init__(self, requester_id: int) -> None:
        super().__init__(timeout=SUPERVISION_VIEW_TIMEOUT_SECONDS, disable_on_timeout=True)
        self.requester_id = requester_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            "Esta consulta pertenece a otro supervisor.",
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
    ) -> None:
        self.finder = finder
        self.policy = policy
        self.guild = guild
        self.member = member
        self.display_name = display_name
        self.period_select = _period_select("supervision_pending_period")
        self.channel_select = _channel_select("supervision_pending_channel", channels)
        super().__init__(
            discord.ui.Label("Periodo", self.period_select),
            discord.ui.Label("Estación", self.channel_select),
            title="Consultar reportes pendientes",
            custom_id=f"supervision_pending:{member.id}",
            timeout=SUPERVISION_VIEW_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _authorize_modal_interaction(
            interaction,
            requester_id=self.member.id,
            policy=self.policy,
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
    ) -> None:
        self.finder = finder
        self.policy = policy
        self.guild = guild
        self.member = member
        self.display_name = display_name
        self.period_select = _period_select("supervision_ranking_period")
        self.metric_select = discord.ui.StringSelect(
            custom_id="supervision_ranking_metric",
            options=[
                discord.SelectOption(
                    label=label,
                    value=metric.value,
                    default=metric is SupervisorRankingMetric.APPROVED,
                )
                for metric, label in RANKING_METRIC_LABELS.items()
            ],
        )
        self.channel_select = _channel_select("supervision_ranking_channel", channels)
        super().__init__(
            discord.ui.Label("Periodo", self.period_select),
            discord.ui.Label("Ordenar por", self.metric_select),
            discord.ui.Label("Estación", self.channel_select),
            title="Ranking de reportes",
            custom_id=f"supervision_ranking:{member.id}",
            timeout=SUPERVISION_VIEW_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _authorize_modal_interaction(
            interaction,
            requester_id=self.member.id,
            policy=self.policy,
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
            )


class PendingReportsPaginationView(SupervisorRequesterView):
    def __init__(
        self,
        *,
        requester_id: int,
        result: PersonalReportSearchResult,
        query: PersonalReportQuery,
        display_name: str,
    ) -> None:
        super().__init__(requester_id)
        self.result = result
        self.query = query
        self.display_name = display_name
        self.page = 0
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
    ) -> None:
        super().__init__(requester_id)
        self.ranking = ranking
        self.metric = metric
        self.query = query
        self.truncated = truncated
        self.display_name = display_name
        self.page = 0
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
) -> discord.Embed:
    page_count = max(1, ceil(len(result.reports) / PENDING_REPORTS_PER_PAGE))
    safe_page = min(max(page, 0), page_count - 1)
    start = safe_page * PENDING_REPORTS_PER_PAGE
    reports = result.reports[start : start + PENDING_REPORTS_PER_PAGE]
    channel_scope = "Todas las estaciones" if query.channel_id is None else f"<#{query.channel_id}>"
    embed = discord.Embed(
        title="⏳ Reportes pendientes",
        description=(
            f"**Periodo:** {PERIOD_LABELS[query.period]}\n"
            f"**Estación:** {channel_scope}\n"
            "Incluye expedientes sin estado terminal y revisiones actualmente en curso."
        ),
        color=discord.Color.gold(),
    )
    if not reports:
        embed.add_field(
            name="Guardia al día",
            value="No encontré reportes pendientes con estos filtros.",
            inline=False,
        )
    for report in reports:
        icon, status_label = STATUS_PRESENTATION[report.status]
        author = f"<@{report.author_id}>" if report.author_id is not None else "Autor desconocido"
        links = f"[Abrir publicación]({report.post_url})"
        if report.thread_url:
            links += f" · [Abrir hilo]({report.thread_url})"
        embed.add_field(
            name=f"{icon} {report.title}"[:256],
            value=(
                f"**{status_label}** · {author} · <#{report.channel_id}> · "
                f"{report.created_at.strftime('%d/%m/%y')}\n{links}"
            ),
            inline=False,
        )
    footer = (
        f"Página {safe_page + 1}/{page_count} · {len(result.reports)} resultado(s) · "
        f"{display_name}"
    )
    if result.truncated:
        footer += " · Búsqueda limitada"
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
) -> discord.Embed:
    page_count = max(1, ceil(len(ranking) / RANKING_ENTRIES_PER_PAGE))
    safe_page = min(max(page, 0), page_count - 1)
    start = safe_page * RANKING_ENTRIES_PER_PAGE
    entries = ranking[start : start + RANKING_ENTRIES_PER_PAGE]
    channel_scope = "Todas las estaciones" if query.channel_id is None else f"<#{query.channel_id}>"
    lines = []
    for offset, entry in enumerate(entries):
        position = start + offset + 1
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(position, f"**{position}.**")
        lines.append(
            f"{medal} <@{entry.author_id}> — **{_ranking_value(entry, metric)}**\n"
            f"↳ 📂 {entry.total} · ✅ {entry.approved} · ❌ {entry.corrections_required} · "
            f"⏳ {entry.pending} · ⚠️ {entry.inconclusive} · {entry.approval_rate:.1f}%"
        )
    embed = discord.Embed(
        title="🏅 Ranking de reportes",
        description=(
            f"**Orden:** {RANKING_METRIC_LABELS[metric]}\n"
            f"**Periodo:** {PERIOD_LABELS[query.period]}\n"
            f"**Estación:** {channel_scope}\n\n"
            + ("\n".join(lines) if lines else "No encontré reportes para construir el ranking.")
        ),
        color=discord.Color.gold(),
    )
    footer = f"Página {safe_page + 1}/{page_count} · {len(ranking)} miembro(s) · {display_name}"
    if truncated:
        footer += " · Snapshot limitado"
    embed.set_footer(text=footer)
    return embed


async def _authorize_modal_interaction(
    interaction: discord.Interaction,
    *,
    requester_id: int,
    policy: SupervisorRolePolicy,
) -> bool:
    if (
        interaction.user.id == requester_id
        and isinstance(interaction.user, discord.Member)
        and policy.allows(interaction.user)
    ):
        return True
    await interaction.response.send_message(
        build_supervisor_unauthorized_message(),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    return False


def _period_select(custom_id: str) -> discord.ui.Select:
    return discord.ui.StringSelect(
        custom_id=custom_id,
        options=[
            discord.SelectOption(
                label=label,
                value=period.value,
                default=period is PersonalReportPeriod.CURRENT_MONTH,
            )
            for period, label in PERIOD_LABELS.items()
        ],
    )


def _channel_select(
    custom_id: str,
    channels: tuple[discord.TextChannel, ...],
) -> discord.ui.Select:
    return discord.ui.StringSelect(
        custom_id=custom_id,
        options=[
            discord.SelectOption(label="Todas las estaciones", value="all", default=True),
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
        "📁 No pude consultar el archivo de supervisión en este momento. Inténtelo nuevamente.",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


def _ranking_value(
    entry: SupervisorRankingEntry,
    metric: SupervisorRankingMetric,
) -> str:
    if metric is SupervisorRankingMetric.APPROVED:
        return f"{entry.approved} aprobado(s)"
    if metric is SupervisorRankingMetric.CORRECTIONS_REQUIRED:
        return f"{entry.corrections_required} con correcciones"
    if metric is SupervisorRankingMetric.APPROVAL_RATE:
        return f"{entry.approval_rate:.1f}% de aprobación"
    return f"{entry.total} presentado(s)"
