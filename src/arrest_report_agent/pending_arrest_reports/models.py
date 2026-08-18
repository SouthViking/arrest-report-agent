"""Deterministic models for identifying unfinished SARP arrest reports."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, computed_field


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
