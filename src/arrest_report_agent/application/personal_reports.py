"""Pure models for deterministic personal report queries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class PersonalReportStatus(StrEnum):
    """Current report state inferred from this bot's status reaction."""

    PENDING = "pending"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    CORRECTIONS_REQUIRED = "corrections_required"
    INCONCLUSIVE = "inconclusive"


class PersonalReportStatusFilter(StrEnum):
    """User-facing state filters exposed by the Discord modal."""

    ALL = "all"
    PENDING = "pending"
    APPROVED = "approved"
    CORRECTIONS_REQUIRED = "corrections_required"
    INCONCLUSIVE = "inconclusive"

    def includes(self, status: PersonalReportStatus) -> bool:
        if self is PersonalReportStatusFilter.ALL:
            return True
        if self is PersonalReportStatusFilter.PENDING:
            return status in {
                PersonalReportStatus.PENDING,
                PersonalReportStatus.IN_REVIEW,
            }
        return self.value == status.value


class PersonalReportPeriod(StrEnum):
    """Bounded periods supported by deterministic history reads."""

    CURRENT_MONTH = "current_month"
    LAST_7_DAYS = "last_7_days"
    LAST_30_DAYS = "last_30_days"
    LAST_90_DAYS = "last_90_days"

    def starts_at(self, now: datetime) -> datetime:
        if self is PersonalReportPeriod.CURRENT_MONTH:
            return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        days = {
            PersonalReportPeriod.LAST_7_DAYS: 7,
            PersonalReportPeriod.LAST_30_DAYS: 30,
            PersonalReportPeriod.LAST_90_DAYS: 90,
        }[self]
        return now - timedelta(days=days)


@dataclass(frozen=True, slots=True)
class PersonalReportQuery:
    period: PersonalReportPeriod = PersonalReportPeriod.CURRENT_MONTH
    status: PersonalReportStatusFilter = PersonalReportStatusFilter.ALL
    channel_id: int | None = None


@dataclass(frozen=True, slots=True)
class PersonalReport:
    message_id: int
    channel_id: int
    created_at: datetime
    title: str
    status: PersonalReportStatus
    post_url: str
    thread_url: str | None = None
    author_id: int | None = None
    author_display_name: str | None = None


@dataclass(frozen=True, slots=True)
class PersonalReportSearchResult:
    reports: tuple[PersonalReport, ...]
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class PersonalReportSummary:
    total: int
    approved: int
    corrections_required: int
    pending: int
    inconclusive: int

    @classmethod
    def from_reports(cls, reports: tuple[PersonalReport, ...]) -> PersonalReportSummary:
        return cls(
            total=len(reports),
            approved=sum(report.status is PersonalReportStatus.APPROVED for report in reports),
            corrections_required=sum(
                report.status is PersonalReportStatus.CORRECTIONS_REQUIRED for report in reports
            ),
            pending=sum(
                report.status in {PersonalReportStatus.PENDING, PersonalReportStatus.IN_REVIEW}
                for report in reports
            ),
            inconclusive=sum(
                report.status is PersonalReportStatus.INCONCLUSIVE for report in reports
            ),
        )

    @property
    def approval_rate(self) -> float:
        if not self.total:
            return 0.0
        return self.approved / self.total * 100
