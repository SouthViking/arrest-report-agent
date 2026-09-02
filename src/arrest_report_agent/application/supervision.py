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
    unreviewed: int
    in_review: int
    inconclusive: int
    report_urls: tuple[str, ...] = ()

    @property
    def pending(self) -> int:
        """Return every non-terminal report that is not awaiting corrections."""

        return self.unreviewed + self.in_review

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
    report_urls: dict[int, list[str]] = {}
    ordered_reports = sorted(
        reports,
        key=lambda report: (report.created_at, report.message_id),
        reverse=True,
    )
    for report in ordered_reports:
        if report.author_id is None:
            continue
        row = mutable.setdefault(
            report.author_id,
            {
                "display_name": report.author_display_name or f"Usuario {report.author_id}",
                "total": 0,
                "approved": 0,
                "corrections_required": 0,
                "unreviewed": 0,
                "in_review": 0,
                "inconclusive": 0,
            },
        )
        urls = report_urls.setdefault(report.author_id, [])
        if report.post_url and report.post_url not in urls:
            urls.append(report.post_url)
        row["total"] = int(row["total"]) + 1
        if report.status is PersonalReportStatus.APPROVED:
            row["approved"] = int(row["approved"]) + 1
        elif report.status is PersonalReportStatus.CORRECTIONS_REQUIRED:
            row["corrections_required"] = int(row["corrections_required"]) + 1
        elif report.status is PersonalReportStatus.INCONCLUSIVE:
            row["inconclusive"] = int(row["inconclusive"]) + 1
        elif report.status is PersonalReportStatus.IN_REVIEW:
            row["in_review"] = int(row["in_review"]) + 1
        elif report.status is PersonalReportStatus.PENDING:
            row["unreviewed"] = int(row["unreviewed"]) + 1

    entries = [
        SupervisorRankingEntry(
            author_id=author_id,
            display_name=str(row["display_name"]),
            total=int(row["total"]),
            approved=int(row["approved"]),
            corrections_required=int(row["corrections_required"]),
            unreviewed=int(row["unreviewed"]),
            in_review=int(row["in_review"]),
            inconclusive=int(row["inconclusive"]),
            report_urls=tuple(report_urls[author_id]),
        )
        for author_id, row in mutable.items()
    ]
    entries.sort(key=lambda entry: _ranking_sort_key(entry, metric))
    return tuple(entries)


def build_open_report_ranking(
    reports: tuple[PersonalReport, ...],
) -> tuple[SupervisorRankingEntry, ...]:
    """Rank authors by reports whose current Discord state has not reached approval."""

    open_reports = tuple(report for report in reports if report.status is not PersonalReportStatus.APPROVED)
    return build_supervisor_ranking(
        open_reports,
        SupervisorRankingMetric.SUBMITTED,
    )


def build_approved_report_ranking(
    reports: tuple[PersonalReport, ...],
) -> tuple[SupervisorRankingEntry, ...]:
    """Rank authors with at least one currently approved report."""

    ranking = build_supervisor_ranking(
        reports,
        SupervisorRankingMetric.APPROVED,
    )
    return tuple(entry for entry in ranking if entry.approved > 0)


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
