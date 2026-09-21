"""Deterministic models for identifying incomplete arrest-report workflows."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator


class PendingReportStatus(StrEnum):
    """Age-based state of one pending report."""

    ON_TIME = "on_time"
    NEAR_DEADLINE = "near_deadline"
    OVER_NORMAL_LIMIT = "over_normal_limit"
    OVER_MAXIMUM_LIMIT = "over_maximum_limit"


class PendingReportSeverity(StrEnum):
    """Operational priority derived from completeness and elapsed time."""

    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class PendingReportReason(StrEnum):
    """Deterministic signal that made a PCU narrative look unfinished."""

    EMPTY = "empty"
    PLACEHOLDER = "placeholder"
    REPEATED_CONTENT = "repeated_content"
    SHORT_NONSTANDARD = "short_nonstandard"


class ArrestReviewStatus(StrEnum):
    """Approval state of the newest Discord review post for one report."""

    MISSING = "missing"
    PENDING = "pending"
    APPROVED = "approved"


class ArrestReviewSource(StrEnum):
    """Publication mechanism used by one Discord review root."""

    STRUCTURED = "structured"
    LEGACY = "legacy"


class FrozenPendingReportModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArrestReportReviewReference(FrozenPendingReportModel):
    """Newest structured or legacy Discord review post for one SARP report."""

    report_id: int = Field(gt=0)
    source: ArrestReviewSource
    approved: bool
    guild_id: int = Field(gt=0)
    parent_channel_id: int = Field(gt=0)
    root_message_id: int = Field(gt=0)
    reviewed_at: datetime

    @property
    def jump_url(self) -> str:
        return (
            f"https://discord.com/channels/{self.guild_id}/"
            f"{self.parent_channel_id}/{self.root_message_id}"
        )


class PendingArrestReport(FrozenPendingReportModel):
    report_id: int = Field(gt=0)
    officer_name: str = Field(min_length=1, max_length=100)
    suspect_name: str = Field(min_length=1, max_length=100)
    created_at: datetime
    age_hours: int = Field(ge=0)
    status: PendingReportStatus
    pcu_reason: PendingReportReason | None = None
    review_status: ArrestReviewStatus
    review_url: str | None = Field(default=None, min_length=1)
    reviewed_at: datetime | None = None
    review_age_hours: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_pending_state(self) -> PendingArrestReport:
        if self.pcu_reason is None and self.review_status is ArrestReviewStatus.APPROVED:
            raise ValueError("a completed report cannot be listed as pending")
        if self.review_status is ArrestReviewStatus.MISSING and self.review_url is not None:
            raise ValueError("a missing review cannot have a Discord URL")
        if self.review_status is not ArrestReviewStatus.MISSING and self.review_url is None:
            raise ValueError("an existing review requires its Discord URL")
        if self.review_status is ArrestReviewStatus.MISSING and (
            self.reviewed_at is not None or self.review_age_hours is not None
        ):
            raise ValueError("a missing review cannot have review timing")
        if self.review_status is not ArrestReviewStatus.MISSING and (
            self.reviewed_at is None or self.review_age_hours is None
        ):
            raise ValueError("an existing review requires review timing")
        return self


class PendingArrestReportScan(FrozenPendingReportModel):
    faction_id: int = Field(gt=0)
    date_from: date
    date_to: date
    total_reports: int = Field(ge=0)
    pending_reports: tuple[PendingArrestReport, ...] = ()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pending_count(self) -> int:
        return len(self.pending_reports)

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
