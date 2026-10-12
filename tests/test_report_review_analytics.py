from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config

from arrest_report_agent.application import PersonalReportPeriod
from arrest_report_agent.domain import (
    ConversationMessage,
    ConversationResponseRoute,
    FindingSeverity,
    InteractionMode,
    ReviewCase,
    ReviewFinding,
    ReviewIssueType,
    ReviewPublication,
)
from arrest_report_agent.infrastructure.postgres import PostgresClient, PostgresClientConfig
from arrest_report_agent.infrastructure.postgres.schema import discord_guilds
from arrest_report_agent.review_analytics import (
    RecordingReviewPublisher,
    ReportErrorAnalyticsEntry,
    ReportErrorAnalyticsResult,
    ReportReviewRepository,
)
from arrest_report_agent.review_analytics.discord import build_report_errors_embed


def _case(
    *,
    guild_id: int,
    channel_id: int,
    root_message_id: int,
    trigger_message_id: int,
) -> ReviewCase:
    created_at = datetime.now(UTC)
    return ReviewCase(
        guild_id=guild_id,
        parent_channel_id=channel_id,
        channel_id=root_message_id,
        thread_id=root_message_id,
        root_message_id=root_message_id,
        trigger_message_id=trigger_message_id,
        requester_user_id=800,
        requester_display_name="Supervisor",
        interaction_mode=InteractionMode.THREAD_FOLLOW_UP,
        messages=[
            ConversationMessage(
                id=root_message_id,
                author_id=700,
                author_display="Deputy Test",
                author_is_bot=False,
                content="Reporte de prueba",
                created_at=created_at,
            ),
            ConversationMessage(
                id=trigger_message_id,
                author_id=800,
                author_display="Supervisor",
                author_is_bot=False,
                content="Revísalo",
                created_at=created_at,
                is_trigger=True,
            ),
        ],
        attachments=[],
    )


def _publication(
    issue_type: ReviewIssueType,
    severity: FindingSeverity,
) -> ReviewPublication:
    return ReviewPublication(
        verdict=("corrections_required" if severity is FindingSeverity.REQUIRED else "approved"),
        summary="Resultado estructurado.",
        findings=[
            ReviewFinding(
                issue_type=issue_type,
                severity=severity,
                title="Hallazgo de prueba",
                detail="Se detectó un problema verificable.",
                recommendation="Corrige el punto indicado.",
            )
        ],
    )


def test_review_finding_schema_exposes_closed_issue_taxonomy() -> None:
    schema = ReviewFinding.model_json_schema()

    assert "issue_type" in schema["required"]
    assert set(schema["$defs"]["ReviewIssueType"]["enum"]) == {
        issue_type.value for issue_type in ReviewIssueType
    }


def test_report_error_embed_explains_deduplicated_counts(persona) -> None:
    ui = persona.messages.interface.supervision
    navigation = persona.messages.interface.navigation
    result = ReportErrorAnalyticsResult(
        entries=(
            ReportErrorAnalyticsEntry(
                issue_type=ReviewIssueType.ORTHOGRAPHY,
                display_name="Ortografía y puntuación",
                report_count=3,
                finding_count=5,
                required_reports=2,
                warning_reports=1,
                observation_reports=0,
            ),
        ),
        period_started_at=datetime(2026, 8, 1, tzinfo=UTC),
        total_reports=4,
        reports_with_findings=3,
        total_attempts=7,
        total_issue_types=1,
        channel_id=123,
        severity=None,
    )

    embed = build_report_errors_embed(
        result,
        period=PersonalReportPeriod.CURRENT_MONTH,
        display_name=persona.identity.display_name,
        ui=ui,
        navigation=navigation,
    )

    assert "4 expediente(s)" in embed.description
    assert "7 intento(s)" in embed.description
    assert "3 reporte(s)" in embed.fields[0].name
    assert "🔴 2" in embed.fields[0].value
    assert "5 hallazgo(s)" in embed.fields[0].value


@pytest.mark.asyncio
async def test_recording_publisher_records_before_discord_and_marks_success() -> None:
    events: list[str] = []

    class Repository:
        async def begin_review(self, **_kwargs):
            events.append("begin")
            return SimpleNamespace(
                id="00000000-0000-0000-0000-000000000001",
                should_publish=True,
                attempt_number=1,
                status=SimpleNamespace(value="publishing"),
            )

        async def mark_published(self, _review_id):
            events.append("published")

        async def mark_failed(self, _review_id):
            events.append("failed")

    class Publisher:
        async def publish(self, _publication):
            events.append("discord")

        async def mark_review_started(self):
            return None

        async def send_message(self, _content, response_route=ConversationResponseRoute.REPLY):
            return response_route

        async def set_message_reaction(self, _message_id, _emoji):
            return "added"

        async def publish_failure(self, _public_message, *, notify_manual_review: bool = False):
            return None

    case = _case(guild_id=1, channel_id=2, root_message_id=3, trigger_message_id=4)
    publisher = RecordingReviewPublisher(
        Publisher(),  # type: ignore[arg-type]
        Repository(),  # type: ignore[arg-type]
        case,
        model="gpt-test",
        reasoning_effort="low",
        persona_id="walter_vigil",
        persona_revision=1,
        app_version="0.0.0",
    )

    await publisher.publish(_publication(ReviewIssueType.ORTHOGRAPHY, FindingSeverity.REQUIRED))

    assert events == ["begin", "discord", "published"]


@pytest.mark.asyncio
async def test_report_review_repository_deduplicates_roots_but_preserves_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("requires TEST_DATABASE_URL with an isolated PostgreSQL database")
    monkeypatch.setenv("DATABASE_URL", database_url)
    await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    guild_id = 876543210123456780
    channel_id = 876543210123456781
    second_channel_id = 876543210123456782
    try:
        await client.execute(discord_guilds.delete().where(discord_guilds.c.discord_id == guild_id))
        await client.execute(discord_guilds.insert().values(discord_id=guild_id, name="Review analytics QA"))
        repository = ReportReviewRepository(client)

        async def record(
            case: ReviewCase,
            publication: ReviewPublication,
            *,
            published: bool = True,
        ) -> None:
            recording = await repository.begin_review(
                case=case,
                publication=publication,
                report_author_user_id=700,
                model="gpt-test",
                reasoning_effort="low",
                prompt_version="test",
                persona_id="walter_vigil",
                persona_revision=1,
                app_version="0.0.0",
            )
            if published:
                await repository.mark_published(recording.id)
            else:
                await repository.mark_failed(recording.id)

        required_orthography = _publication(
            ReviewIssueType.ORTHOGRAPHY,
            FindingSeverity.REQUIRED,
        )
        await record(
            _case(
                guild_id=guild_id,
                channel_id=channel_id,
                root_message_id=100,
                trigger_message_id=101,
            ),
            required_orthography,
        )
        await record(
            _case(
                guild_id=guild_id,
                channel_id=channel_id,
                root_message_id=100,
                trigger_message_id=102,
            ),
            required_orthography,
        )
        await record(
            _case(
                guild_id=guild_id,
                channel_id=channel_id,
                root_message_id=200,
                trigger_message_id=201,
            ),
            _publication(ReviewIssueType.ORTHOGRAPHY, FindingSeverity.WARNING),
        )
        await record(
            _case(
                guild_id=guild_id,
                channel_id=second_channel_id,
                root_message_id=300,
                trigger_message_id=301,
            ),
            _publication(ReviewIssueType.CALLSIGN_FORMAT, FindingSeverity.OBSERVATION),
            published=False,
        )

        result = await repository.aggregate_errors(
            guild_id=guild_id,
            period=PersonalReportPeriod.CURRENT_MONTH,
            channel_id=None,
            severity=None,
            limit=len(ReviewIssueType),
        )
        required = await repository.aggregate_errors(
            guild_id=guild_id,
            period=PersonalReportPeriod.CURRENT_MONTH,
            channel_id=channel_id,
            severity=FindingSeverity.REQUIRED,
            limit=5,
        )

        assert result.total_reports == 2
        assert result.total_attempts == 3
        assert result.entries[0].issue_type is ReviewIssueType.ORTHOGRAPHY
        assert result.entries[0].report_count == 2
        assert result.entries[0].finding_count == 3
        assert required.entries[0].report_count == 1
        assert required.entries[0].finding_count == 2
    finally:
        await client.execute(discord_guilds.delete().where(discord_guilds.c.discord_id == guild_id))
        await client.close()
