from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from arrest_report_agent.application import (
    PersonalReport,
    PersonalReportQuery,
    PersonalReportSearchResult,
    PersonalReportStatus,
    SupervisorRankingMetric,
    build_supervisor_ranking,
)
from arrest_report_agent.config import SnowflakeAllowlist
from arrest_report_agent.infrastructure.discord.supervision import (
    SupervisorRolePolicy,
    build_pending_reports_embed,
    build_ranking_embed,
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
    assert (
        SupervisorRolePolicy(SnowflakeAllowlist(frozenset({20, 30}))).allows(member)
        is True
    )
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


def test_supervision_embeds_expose_navigation_and_current_snapshot() -> None:
    reports = (
        report(1, 100, PersonalReportStatus.PENDING),
        report(2, 200, PersonalReportStatus.APPROVED),
    )
    pending = build_pending_reports_embed(
        PersonalReportSearchResult(reports=(reports[0],)),
        PersonalReportQuery(),
        display_name="Sgt. Rivera",
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
    )
    assert pending.title == "⏳ Reportes pendientes"
    assert "<@100>" in pending.fields[0].value
    assert "04/08/26" in pending.fields[0].value
    assert "[Abrir publicación]" in pending.fields[0].value
    assert ranking.title == "🏅 Ranking de reportes"
    assert "<@100>" in ranking.description
