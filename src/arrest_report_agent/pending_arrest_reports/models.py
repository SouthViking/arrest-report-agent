"""Deterministic models for identifying unfinished SARP arrest reports."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from arrest_report_agent.domain import ReviewVerdict


class PendingReportStatus(StrEnum):
    """Age-based state of one unfinished report."""

    ON_TIME = "on_time"
    NEAR_DEADLINE = "near_deadline"
    OVER_NORMAL_LIMIT = "over_normal_limit"
    OVER_MAXIMUM_LIMIT = "over_maximum_limit"


class PendingReportReason(StrEnum):
    """Deterministic signal that made a report look unfinished."""

    EMPTY = "empty"
    PLACEHOLDER = "placeholder"
    REPEATED_CONTENT = "repeated_content"
    SHORT_NONSTANDARD = "short_nonstandard"


class FrozenPendingReportModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PendingArrestReport(FrozenPendingReportModel):
    report_id: int = Field(gt=0)
    officer_name: str = Field(min_length=1, max_length=100)
    suspect_name: str = Field(min_length=1, max_length=100)
    created_at: datetime
    status: PendingReportStatus
    reason: PendingReportReason


class PublishedArrestReview(FrozenPendingReportModel):
    """Latest published full review linked to one SARP arrest report."""

    report_id: int = Field(gt=0)
    verdict: ReviewVerdict
    guild_id: int = Field(gt=0)
    parent_channel_id: int = Field(gt=0)
    root_message_id: int = Field(gt=0)
    published_at: datetime

    @property
    def jump_url(self) -> str:
        return (
            f"https://discord.com/channels/{self.guild_id}/"
            f"{self.parent_channel_id}/{self.root_message_id}"
        )


class ArrestReportDiscordReference(FrozenPendingReportModel):
    """Most recent Discord root bound to one SARP arrest report."""

    report_id: int = Field(gt=0)
    guild_id: int = Field(gt=0)
    parent_channel_id: int = Field(gt=0)
    root_message_id: int = Field(gt=0)
    recorded_at: datetime

    @property
    def jump_url(self) -> str:
        return (
            f"https://discord.com/channels/{self.guild_id}/"
            f"{self.parent_channel_id}/{self.root_message_id}"
        )


class UnapprovedArrestReport(FrozenPendingReportModel):
    """SARP report whose latest formal Discord review is not approved."""

    report_id: int = Field(gt=0)
    officer_name: str = Field(min_length=1, max_length=100)
    suspect_name: str = Field(min_length=1, max_length=100)
    created_at: datetime
    review_verdict: ReviewVerdict | None = None
    review_url: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_review_reference(self) -> UnapprovedArrestReport:
        if self.review_verdict is ReviewVerdict.APPROVED:
            raise ValueError("approved reports cannot be listed as unapproved")
        if self.review_verdict is not None and self.review_url is None:
            raise ValueError("a persisted review verdict requires its Discord URL")
        return self


class PendingArrestReportScan(FrozenPendingReportModel):
    faction_id: int = Field(gt=0)
    date_from: date
    date_to: date
    total_reports: int = Field(ge=0)
    pending_reports: tuple[PendingArrestReport, ...] = ()
    unapproved_reviews_included: bool = False
    unapproved_reports: tuple[UnapprovedArrestReport, ...] = ()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pending_count(self) -> int:
        return len(self.pending_reports)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def unapproved_count(self) -> int:
        return len(self.unapproved_reports)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def completed_count(self) -> int:
        return max(0, self.total_reports - self.pending_count)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def compliance_percentage(self) -> float | None:
        if self.total_reports == 0:
            return None
        return round(self.completed_count * 100 / self.total_reports, 1)


class PendingReportScanTooLargeError(RuntimeError):
    """The selected period contains more reports than one bounded scan permits."""

    def __init__(self, total: int, limit: int) -> None:
        super().__init__(f"pending report scan contains {total} records; limit is {limit}")
        self.total = total
        self.limit = limit
