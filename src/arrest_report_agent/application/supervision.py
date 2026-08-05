"""Pure aggregation models for deterministic supervision commands."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .personal_reports import PersonalReport, PersonalReportStatus


class SupervisorRankingMetric(StrEnum):
    SUBMITTED = "submitted"
    APPROVED = "approved"
    CORRECTIONS_REQUIRED = "corrections_required"
    APPROVAL_RATE = "approval_rate"


@dataclass(frozen=True, slots=True)
class SupervisorRankingEntry:
    author_id: int
    display_name: str
    total: int
    approved: int
    corrections_required: int
    pending: int
    inconclusive: int

    @property
    def approval_rate(self) -> float:
        if not self.total:
            return 0.0
        return self.approved / self.total * 100


def build_supervisor_ranking(
    reports: tuple[PersonalReport, ...],
    metric: SupervisorRankingMetric,
) -> tuple[SupervisorRankingEntry, ...]:
    """Aggregate the current Discord snapshot by report author."""

    mutable: dict[int, dict[str, int | str]] = {}
    for report in reports:
        if report.author_id is None:
            continue
        row = mutable.setdefault(
            report.author_id,
            {
                "display_name": report.author_display_name or f"Usuario {report.author_id}",
                "total": 0,
                "approved": 0,
                "corrections_required": 0,
                "pending": 0,
                "inconclusive": 0,
            },
        )
        row["total"] = int(row["total"]) + 1
        if report.status is PersonalReportStatus.APPROVED:
            row["approved"] = int(row["approved"]) + 1
        elif report.status is PersonalReportStatus.CORRECTIONS_REQUIRED:
            row["corrections_required"] = int(row["corrections_required"]) + 1
        elif report.status is PersonalReportStatus.INCONCLUSIVE:
            row["inconclusive"] = int(row["inconclusive"]) + 1
        else:
            row["pending"] = int(row["pending"]) + 1

    entries = [
        SupervisorRankingEntry(
            author_id=author_id,
            display_name=str(row["display_name"]),
            total=int(row["total"]),
            approved=int(row["approved"]),
            corrections_required=int(row["corrections_required"]),
            pending=int(row["pending"]),
            inconclusive=int(row["inconclusive"]),
        )
        for author_id, row in mutable.items()
    ]
    entries.sort(key=lambda entry: _ranking_sort_key(entry, metric))
    return tuple(entries)


def _ranking_sort_key(
    entry: SupervisorRankingEntry,
    metric: SupervisorRankingMetric,
) -> tuple[float | int, ...]:
    if metric is SupervisorRankingMetric.APPROVED:
        return (-entry.approved, -entry.approval_rate, -entry.total, entry.author_id)
    if metric is SupervisorRankingMetric.CORRECTIONS_REQUIRED:
        return (-entry.corrections_required, -entry.total, entry.author_id)
    if metric is SupervisorRankingMetric.APPROVAL_RATE:
        return (-entry.approval_rate, -entry.approved, -entry.total, entry.author_id)
    return (-entry.total, -entry.approved, entry.author_id)
