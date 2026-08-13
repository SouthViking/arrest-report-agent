"""Typed values crossing the report-review analytics boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from arrest_report_agent.domain import FindingSeverity, ReviewIssueType


class ReviewRecordingStatus(StrEnum):
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ReviewRecording:
    id: UUID
    attempt_number: int
    status: ReviewRecordingStatus
    should_publish: bool


@dataclass(frozen=True, slots=True)
class ReportErrorAnalyticsEntry:
    issue_type: ReviewIssueType
    display_name: str
    report_count: int
    finding_count: int
    required_reports: int
    warning_reports: int
    observation_reports: int


@dataclass(frozen=True, slots=True)
class ReportErrorAnalyticsResult:
    entries: tuple[ReportErrorAnalyticsEntry, ...]
    period_started_at: datetime
    total_reports: int
    reports_with_findings: int
    total_attempts: int
    total_issue_types: int
    channel_id: int | None
    severity: FindingSeverity | None
