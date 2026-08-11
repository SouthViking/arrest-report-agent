from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import discord
import pytest

from arrest_report_agent.application import (
    PersonalReport,
    PersonalReportPeriod,
    PersonalReportQuery,
    PersonalReportSearchResult,
    PersonalReportStatus,
    PersonalReportStatusFilter,
    SupervisorRankingMetric,
    build_approved_report_ranking,
    build_open_report_ranking,
    build_supervisor_ranking,
)
from arrest_report_agent.config import SnowflakeAllowlist
from arrest_report_agent.infrastructure.discord.supervision import (
    HonorRankingModal,
    HonorRankingSection,
    ShameRankingModal,
    ShameRankingSection,
    SupervisorRolePolicy,
    build_honor_ranking_embed,
    build_pending_reports_embed,
    build_ranking_embed,
    build_shame_ranking_embed,
    build_supervisor_unauthorized_message,
)


def report(
    message_id: int,
    author_id: int,
    status: PersonalReportStatus,
) -> PersonalReport:
    return PersonalReport(
        message_id=message_id,
        channel_id=200,
        created_at=datetime(2026, 8, 4, tzinfo=UTC),
        title=f"Expediente {message_id}",
        status=status,
        post_url=f"https://discord.com/channels/1/200/{message_id}",
        thread_url=f"https://discord.com/channels/1/{message_id}",
        author_id=author_id,
        author_display_name=f"Deputy {author_id}",
    )


def test_supervisor_role_policy_supports_open_and_restricted_modes() -> None:
    member = SimpleNamespace(roles=[SimpleNamespace(id=10), SimpleNamespace(id=20)])

    assert SupervisorRolePolicy(SnowflakeAllowlist(None)).allows(member) is True
    assert SupervisorRolePolicy(SnowflakeAllowlist(frozenset({20, 30}))).allows(member) is True
    assert SupervisorRolePolicy(SnowflakeAllowlist(frozenset({30}))).allows(member) is False


def test_supervisor_unauthorized_message_comes_from_persona(persona) -> None:
    assert build_supervisor_unauthorized_message(persona.messages.access) == (
        persona.messages.access.supervisor_unauthorized
    )


def test_ranking_aggregates_current_status_and_sorts_by_requested_metric() -> None:
    reports = (
        report(1, 100, PersonalReportStatus.APPROVED),
        report(2, 100, PersonalReportStatus.APPROVED),
        report(3, 100, PersonalReportStatus.CORRECTIONS_REQUIRED),
        report(4, 200, PersonalReportStatus.CORRECTIONS_REQUIRED),
        report(5, 200, PersonalReportStatus.PENDING),
    )

    approved = build_supervisor_ranking(reports, SupervisorRankingMetric.APPROVED)
    corrections = build_supervisor_ranking(
        reports,
        SupervisorRankingMetric.CORRECTIONS_REQUIRED,
    )

    assert [entry.author_id for entry in approved] == [100, 200]
    assert approved[0].approval_rate == 2 / 3 * 100
    assert [entry.author_id for entry in corrections] == [100, 200]
    assert corrections[0].corrections_required == corrections[1].corrections_required == 1


def test_open_report_ranking_excludes_approved_and_sorts_descending() -> None:
    reports = (
        report(1, 100, PersonalReportStatus.APPROVED),
        report(2, 100, PersonalReportStatus.CORRECTIONS_REQUIRED),
        report(3, 200, PersonalReportStatus.PENDING),
        report(4, 200, PersonalReportStatus.IN_REVIEW),
        report(5, 200, PersonalReportStatus.INCONCLUSIVE),
        report(6, 300, PersonalReportStatus.APPROVED),
    )

    ranking = build_open_report_ranking(reports)

    assert [entry.author_id for entry in ranking] == [200, 100]
    assert ranking[0].total == 3
    assert ranking[0].unreviewed == 1
    assert ranking[0].in_review == 1
    assert ranking[0].pending == 2
    assert ranking[0].inconclusive == 1
    assert ranking[0].approved == 0
    assert ranking[1].corrections_required == 1


def test_approved_report_ranking_requires_approval_and_preserves_context() -> None:
    reports = (
        report(1, 100, PersonalReportStatus.APPROVED),
        report(2, 100, PersonalReportStatus.APPROVED),
        report(3, 100, PersonalReportStatus.CORRECTIONS_REQUIRED),
        report(4, 200, PersonalReportStatus.APPROVED),
        report(5, 200, PersonalReportStatus.PENDING),
        report(6, 300, PersonalReportStatus.CORRECTIONS_REQUIRED),
    )

    ranking = build_approved_report_ranking(reports)

    assert [entry.author_id for entry in ranking] == [100, 200]
    assert ranking[0].approved == 2
    assert ranking[0].total == 3
    assert ranking[0].approval_rate == pytest.approx(66.7, abs=0.1)
    assert ranking[1].approved == 1
    assert ranking[1].total == 2


def test_supervision_embeds_expose_navigation_and_current_snapshot(persona) -> None:
    reports = (
        report(1, 100, PersonalReportStatus.PENDING),
        report(2, 200, PersonalReportStatus.APPROVED),
    )
    pending = build_pending_reports_embed(
        PersonalReportSearchResult(reports=(reports[0],)),
        PersonalReportQuery(),
        display_name="Sgt. Rivera",
        ui=persona.messages.interface.supervision,
        navigation=persona.messages.interface.navigation,
    )
    ranking_entries = build_supervisor_ranking(
        reports,
        SupervisorRankingMetric.SUBMITTED,
    )
    ranking = build_ranking_embed(
        ranking_entries,
        metric=SupervisorRankingMetric.SUBMITTED,
        query=PersonalReportQuery(),
        truncated=False,
        display_name="Sgt. Rivera",
        ui=persona.messages.interface.supervision,
        navigation=persona.messages.interface.navigation,
    )
    assert pending.title == "⏳ Reportes pendientes"
    assert "<@100>" in pending.fields[0].value
    assert "04/08/26" in pending.fields[0].value
    assert "[Abrir publicación]" in pending.fields[0].value
    assert ranking.title == "🏅 Ranking de reportes"
    assert "<@100>" in ranking.description
    assert "📭 1 · ⏳ 0" in ranking.description


def test_shame_embed_is_public_ready_and_counts_only_non_approved(persona) -> None:
    reports = (
        report(1, 100, PersonalReportStatus.CORRECTIONS_REQUIRED),
        report(2, 100, PersonalReportStatus.PENDING),
        report(3, 100, PersonalReportStatus.IN_REVIEW),
        report(4, 200, PersonalReportStatus.INCONCLUSIVE),
    )
    ranking = build_open_report_ranking(reports)

    embed = build_shame_ranking_embed(
        (
            ShameRankingSection(
                channel_id=200,
                channel_name="gnt-arrestos",
                ranking=ranking,
            ),
            ShameRankingSection(
                channel_id=300,
                channel_name="dlm-arrestos",
                ranking=(),
            ),
        ),
        period=PersonalReportPeriod.LAST_90_DAYS,
        truncated=False,
        display_name="Sgt. Rivera",
        ui=persona.messages.interface.supervision,
        navigation=persona.messages.interface.navigation,
    )

    assert embed.title == "🫵 Muro de expedientes sin aprobar"
    assert "Últimos 90 días" in embed.description
    assert [field.name for field in embed.fields] == [
        "📍 #gnt-arrestos",
        "📍 #dlm-arrestos",
    ]
    assert "🥇 <@100>" in embed.fields[0].value
    assert persona.messages.interface.supervision.shame_legend in embed.description
    assert "3 expediente(s) sin aprobar" in embed.fields[0].value
    assert "📭 1 · ⏳ 1 · ❌ 1 · ⚠️ 0" in embed.fields[0].value
    assert persona.messages.interface.supervision.shame_empty_body in embed.fields[1].value
    assert embed.footer.text == "Top 2 de 2 miembro(s) · Sgt. Rivera"


def test_honor_embed_is_public_ready_and_includes_only_approved_authors(persona) -> None:
    reports = (
        report(1, 100, PersonalReportStatus.APPROVED),
        report(2, 100, PersonalReportStatus.APPROVED),
        report(3, 100, PersonalReportStatus.CORRECTIONS_REQUIRED),
        report(4, 200, PersonalReportStatus.APPROVED),
        report(5, 300, PersonalReportStatus.PENDING),
    )
    ranking = build_approved_report_ranking(reports)

    embed = build_honor_ranking_embed(
        (
            HonorRankingSection(
                channel_id=200,
                channel_name="gnt-arrestos",
                ranking=ranking,
            ),
            HonorRankingSection(
                channel_id=300,
                channel_name="dlm-arrestos",
                ranking=(),
            ),
        ),
        period=PersonalReportPeriod.LAST_30_DAYS,
        truncated=False,
        display_name="Sgt. Rivera",
        ui=persona.messages.interface.supervision,
        navigation=persona.messages.interface.navigation,
    )

    assert embed.title == "🏆 Cuadro de honor"
    assert "Últimos 30 días" in embed.description
    assert [field.name for field in embed.fields] == [
        "📍 #gnt-arrestos",
        "📍 #dlm-arrestos",
    ]
    assert "🥇 <@100>" in embed.fields[0].value
    assert "2 aprobado(s) de 3 · 66.7%" in embed.fields[0].value
    assert "<@300>" not in embed.fields[0].value
    assert persona.messages.interface.supervision.honor_empty_body in embed.fields[1].value
    assert embed.footer.text == "Top 2 de 2 miembro(s) · Sgt. Rivera"


@pytest.mark.asyncio
async def test_shame_modal_uses_selected_period_and_publishes_result(persona) -> None:
    class Finder:
        def __init__(self) -> None:
            self.queries: list[PersonalReportQuery] = []

        async def find_all(self, _guild, _member, query):
            self.queries.append(query)
            return PersonalReportSearchResult(reports=(report(1, 100, PersonalReportStatus.PENDING),))

        def visible_channels(self, _guild, _member):
            return (SimpleNamespace(id=200, name="gnt-arrestos"),)

    deferred: list[bool] = []
    sent: list[dict[str, object]] = []

    async def defer(*, ephemeral: bool) -> None:
        deferred.append(ephemeral)

    async def send(**kwargs) -> None:
        sent.append(kwargs)

    finder = Finder()
    member = SimpleNamespace(id=123)
    modal = ShameRankingModal(
        finder=finder,  # type: ignore[arg-type]
        guild=SimpleNamespace(id=1),  # type: ignore[arg-type]
        member=member,  # type: ignore[arg-type]
        display_name="Sgt. Rivera",
        access_messages=persona.messages.access,
        failure_messages=persona.messages.failures,
        ui=persona.messages.interface.supervision,
        navigation=persona.messages.interface.navigation,
    )
    modal.period_select._interaction = SimpleNamespace(data={})  # noqa: SLF001
    modal.period_select._selected_values = ["last_30_days"]  # noqa: SLF001
    interaction = SimpleNamespace(
        id=456,
        user=member,
        response=SimpleNamespace(defer=defer),
        followup=SimpleNamespace(send=send),
    )

    await modal.callback(interaction)  # type: ignore[arg-type]

    assert deferred == [False]
    assert finder.queries == [
        PersonalReportQuery(
            period=PersonalReportPeriod.LAST_30_DAYS,
            status=PersonalReportStatusFilter.ALL,
        )
    ]
    assert len(sent) == 1
    assert sent[0]["ephemeral"] is False
    assert isinstance(sent[0]["allowed_mentions"], discord.AllowedMentions)
    embed = sent[0]["embed"]
    assert isinstance(embed, discord.Embed)
    assert "Últimos 30 días" in embed.description
    assert embed.fields[0].name == "📍 #gnt-arrestos"


@pytest.mark.asyncio
async def test_honor_modal_uses_selected_period_and_publishes_result(persona) -> None:
    class Finder:
        def __init__(self) -> None:
            self.queries: list[PersonalReportQuery] = []

        async def find_all(self, _guild, _member, query):
            self.queries.append(query)
            return PersonalReportSearchResult(reports=(report(1, 100, PersonalReportStatus.APPROVED),))

        def visible_channels(self, _guild, _member):
            return (SimpleNamespace(id=200, name="gnt-arrestos"),)

    deferred: list[bool] = []
    sent: list[dict[str, object]] = []

    async def defer(*, ephemeral: bool) -> None:
        deferred.append(ephemeral)

    async def send(**kwargs) -> None:
        sent.append(kwargs)

    finder = Finder()
    member = SimpleNamespace(id=123)
    modal = HonorRankingModal(
        finder=finder,  # type: ignore[arg-type]
        guild=SimpleNamespace(id=1),  # type: ignore[arg-type]
        member=member,  # type: ignore[arg-type]
        display_name="Sgt. Rivera",
        access_messages=persona.messages.access,
        failure_messages=persona.messages.failures,
        ui=persona.messages.interface.supervision,
        navigation=persona.messages.interface.navigation,
    )
    modal.period_select._interaction = SimpleNamespace(data={})  # noqa: SLF001
    modal.period_select._selected_values = ["last_7_days"]  # noqa: SLF001
    interaction = SimpleNamespace(
        id=456,
        user=member,
        response=SimpleNamespace(defer=defer),
        followup=SimpleNamespace(send=send),
    )

    await modal.callback(interaction)  # type: ignore[arg-type]

    assert deferred == [False]
    assert finder.queries == [
        PersonalReportQuery(
            period=PersonalReportPeriod.LAST_7_DAYS,
            status=PersonalReportStatusFilter.ALL,
        )
    ]
    assert len(sent) == 1
    assert sent[0]["ephemeral"] is False
    assert isinstance(sent[0]["allowed_mentions"], discord.AllowedMentions)
    embed = sent[0]["embed"]
    assert isinstance(embed, discord.Embed)
    assert "Últimos 7 días" in embed.description
    assert "<@100>" in embed.fields[0].value
