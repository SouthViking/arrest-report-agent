"""Transactional PostgreSQL repository for structured review history."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa

from arrest_report_agent.application import PersonalReportPeriod
from arrest_report_agent.domain import FindingSeverity, ReviewCase, ReviewIssueType, ReviewPublication
from arrest_report_agent.infrastructure.postgres import PostgresClient
from arrest_report_agent.infrastructure.postgres.schema import (
    report_issue_types,
    report_review_findings,
    report_reviews,
)

from .models import (
    ReportErrorAnalyticsEntry,
    ReportErrorAnalyticsResult,
    ReviewRecording,
    ReviewRecordingStatus,
)


class ReportReviewRepository:
    """Persist every review attempt and aggregate stable issue categories."""

    def __init__(self, client: PostgresClient) -> None:
        self.client = client

    async def begin_review(
        self,
        *,
        case: ReviewCase,
        publication: ReviewPublication,
        report_author_user_id: int | None,
        model: str,
        reasoning_effort: str,
        prompt_version: str,
        persona_id: str,
        persona_revision: int,
        app_version: str,
    ) -> ReviewRecording:
        async with self.client.transaction() as connection:
            await connection.execute(
                sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"report-review:{case.guild_id}:{case.root_message_id}"},
            )
            existing = (
                (
                    await connection.execute(
                        sa.select(report_reviews)
                        .where(
                            report_reviews.c.guild_id == case.guild_id,
                            report_reviews.c.trigger_message_id == case.trigger_message_id,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                status = ReviewRecordingStatus(str(existing["status"]))
                if status is not ReviewRecordingStatus.FAILED:
                    return ReviewRecording(
                        id=existing["id"],
                        attempt_number=int(existing["attempt_number"]),
                        status=status,
                        should_publish=False,
                    )
                review_id = existing["id"]
                attempt_number = int(existing["attempt_number"])
                await connection.execute(
                    report_review_findings.delete().where(report_review_findings.c.review_id == review_id)
                )
                await connection.execute(
                    report_reviews.update()
                    .where(report_reviews.c.id == review_id)
                    .values(
                        **self._review_values(
                            case=case,
                            publication=publication,
                            report_author_user_id=report_author_user_id,
                            model=model,
                            reasoning_effort=reasoning_effort,
                            prompt_version=prompt_version,
                            persona_id=persona_id,
                            persona_revision=persona_revision,
                            app_version=app_version,
                        ),
                        status=ReviewRecordingStatus.PUBLISHING.value,
                        published_at=None,
                        failed_at=None,
                    )
                )
            else:
                attempt_number = (
                    int(
                        await connection.scalar(
                            sa.select(
                                sa.func.coalesce(sa.func.max(report_reviews.c.attempt_number), 0)
                            ).where(
                                report_reviews.c.guild_id == case.guild_id,
                                report_reviews.c.root_message_id == case.root_message_id,
                            )
                        )
                        or 0
                    )
                    + 1
                )
                review_id = uuid.uuid4()
                await connection.execute(
                    report_reviews.insert().values(
                        id=review_id,
                        **self._review_values(
                            case=case,
                            publication=publication,
                            report_author_user_id=report_author_user_id,
                            model=model,
                            reasoning_effort=reasoning_effort,
                            prompt_version=prompt_version,
                            persona_id=persona_id,
                            persona_revision=persona_revision,
                            app_version=app_version,
                        ),
                        attempt_number=attempt_number,
                        status=ReviewRecordingStatus.PUBLISHING.value,
                    )
                )
            if publication.findings:
                await connection.execute(
                    report_review_findings.insert(),
                    [
                        {
                            "id": uuid.uuid4(),
                            "review_id": review_id,
                            "issue_code": finding.issue_type.value,
                            "severity": finding.severity.value,
                            "position": position,
                        }
                        for position, finding in enumerate(publication.findings)
                    ],
                )
            return ReviewRecording(
                id=review_id,
                attempt_number=attempt_number,
                status=ReviewRecordingStatus.PUBLISHING,
                should_publish=True,
            )

    async def mark_published(self, review_id: uuid.UUID) -> None:
        await self.client.execute(
            report_reviews.update()
            .where(
                report_reviews.c.id == review_id,
                report_reviews.c.status == ReviewRecordingStatus.PUBLISHING.value,
            )
            .values(
                status=ReviewRecordingStatus.PUBLISHED.value,
                published_at=datetime.now(UTC),
                failed_at=None,
            )
        )

    async def mark_failed(self, review_id: uuid.UUID) -> None:
        await self.client.execute(
            report_reviews.update()
            .where(
                report_reviews.c.id == review_id,
                report_reviews.c.status == ReviewRecordingStatus.PUBLISHING.value,
            )
            .values(
                status=ReviewRecordingStatus.FAILED.value,
                failed_at=datetime.now(UTC),
            )
        )

    async def aggregate_errors(
        self,
        *,
        guild_id: int,
        period: PersonalReportPeriod,
        channel_id: int | None,
        severity: FindingSeverity | None,
        limit: int,
        now: datetime | None = None,
    ) -> ReportErrorAnalyticsResult:
        if not 1 <= limit <= len(ReviewIssueType):
            raise ValueError("Report error analytics limit is outside the supported range")
        period_started_at = period.starts_at(now or datetime.now(UTC))
        review_filters = [
            report_reviews.c.guild_id == guild_id,
            report_reviews.c.status == ReviewRecordingStatus.PUBLISHED.value,
            report_reviews.c.created_at >= period_started_at,
        ]
        if channel_id is not None:
            review_filters.append(report_reviews.c.parent_channel_id == channel_id)
        finding_filters = list(review_filters)
        if severity is not None:
            finding_filters.append(report_review_findings.c.severity == severity.value)

        async with self.client.connection() as connection:
            summary = (
                (
                    await connection.execute(
                        sa.select(
                            sa.func.count(sa.distinct(report_reviews.c.root_message_id)).label(
                                "total_reports"
                            ),
                            sa.func.count(report_reviews.c.id).label("total_attempts"),
                        ).where(*review_filters)
                    )
                )
                .mappings()
                .one()
            )
            findings_summary = (
                (
                    await connection.execute(
                        sa.select(
                            sa.func.count(sa.distinct(report_reviews.c.root_message_id)).label(
                                "reports_with_findings"
                            )
                        )
                        .select_from(
                            report_reviews.join(
                                report_review_findings,
                                report_review_findings.c.review_id == report_reviews.c.id,
                            )
                        )
                        .where(*finding_filters)
                    )
                )
                .mappings()
                .one()
            )
            rows = (
                (
                    await connection.execute(
                        sa.select(
                            report_issue_types.c.code,
                            report_issue_types.c.display_name,
                            report_issue_types.c.position,
                            sa.func.count(sa.distinct(report_reviews.c.root_message_id)).label(
                                "report_count"
                            ),
                            sa.func.count(report_review_findings.c.id).label("finding_count"),
                            sa.func.count(sa.distinct(report_reviews.c.root_message_id))
                            .filter(report_review_findings.c.severity == FindingSeverity.REQUIRED.value)
                            .label("required_reports"),
                            sa.func.count(sa.distinct(report_reviews.c.root_message_id))
                            .filter(report_review_findings.c.severity == FindingSeverity.WARNING.value)
                            .label("warning_reports"),
                            sa.func.count(sa.distinct(report_reviews.c.root_message_id))
                            .filter(report_review_findings.c.severity == FindingSeverity.OBSERVATION.value)
                            .label("observation_reports"),
                        )
                        .select_from(
                            report_reviews.join(
                                report_review_findings,
                                report_review_findings.c.review_id == report_reviews.c.id,
                            ).join(
                                report_issue_types,
                                report_issue_types.c.code == report_review_findings.c.issue_code,
                            )
                        )
                        .where(*finding_filters, report_issue_types.c.active.is_(True))
                        .group_by(
                            report_issue_types.c.code,
                            report_issue_types.c.display_name,
                            report_issue_types.c.position,
                        )
                        .order_by(
                            sa.desc("report_count"),
                            sa.desc("finding_count"),
                            report_issue_types.c.position,
                        )
                    )
                )
                .mappings()
                .all()
            )
        entries = tuple(
            ReportErrorAnalyticsEntry(
                issue_type=ReviewIssueType(str(row["code"])),
                display_name=str(row["display_name"]),
                report_count=int(row["report_count"]),
                finding_count=int(row["finding_count"]),
                required_reports=int(row["required_reports"]),
                warning_reports=int(row["warning_reports"]),
                observation_reports=int(row["observation_reports"]),
            )
            for row in rows
        )
        return ReportErrorAnalyticsResult(
            entries=entries[:limit],
            period_started_at=period_started_at,
            total_reports=int(summary["total_reports"]),
            reports_with_findings=int(findings_summary["reports_with_findings"]),
            total_attempts=int(summary["total_attempts"]),
            total_issue_types=len(entries),
            channel_id=channel_id,
            severity=severity,
        )

    @staticmethod
    def _review_values(
        *,
        case: ReviewCase,
        publication: ReviewPublication,
        report_author_user_id: int | None,
        model: str,
        reasoning_effort: str,
        prompt_version: str,
        persona_id: str,
        persona_revision: int,
        app_version: str,
    ) -> dict[str, object]:
        return {
            "guild_id": case.guild_id,
            "parent_channel_id": case.parent_channel_id,
            "thread_id": case.thread_id,
            "root_message_id": case.root_message_id,
            "trigger_message_id": case.trigger_message_id,
            "report_author_user_id": report_author_user_id,
            "requester_user_id": case.requester_user_id,
            "review_scope": publication.review_scope.value,
            "verdict": publication.verdict.value,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "prompt_version": prompt_version,
            "persona_id": persona_id,
            "persona_revision": persona_revision,
            "app_version": app_version,
        }
