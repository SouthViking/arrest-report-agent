"""Bounded deterministic scanner for unfinished SARP arrest reports."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from arrest_report_agent.domain import ReviewVerdict
from arrest_report_agent.infrastructure.sarp import SarpClient, SarpFactionArrestSummary

from .models import (
    ArrestReportDiscordReference,
    PendingArrestReport,
    PendingArrestReportScan,
    PendingReportReason,
    PendingReportScanTooLargeError,
    PendingReportStatus,
    PublishedArrestReview,
    UnapprovedArrestReport,
)

PAGE_SIZE = 50
DEFAULT_MAX_RECORDS = 1_000
STANDARD_REPORT_PREFIX = re.compile(r"^yo\b\s*,?", re.IGNORECASE)
PLACEHOLDER_ONLY = re.compile(
    r"^(?:pendiente|rellenar|relleno|completar|por\s+(?:a[ñn]adir|agregar|rellenar)|"
    r"borrar|todo|tbd|test|desarrollo+)[\s.!?,;:-]*$",
    re.IGNORECASE,
)
REPEATED_CHARACTER = re.compile(r"([a-záéíóúñ])\1{4,}", re.IGNORECASE)
MAX_SHORT_NONSTANDARD_CHARACTERS = 120


class PendingArrestReviewLookup(Protocol):
    async def latest_published_full_reviews(
        self,
        *,
        guild_id: int,
        report_ids: Collection[int],
        channel_ids: Collection[int] | None,
    ) -> Mapping[int, PublishedArrestReview]: ...

    async def latest_report_references(
        self,
        *,
        guild_id: int,
        report_ids: Collection[int],
        channel_ids: Collection[int] | None,
    ) -> Mapping[int, ArrestReportDiscordReference]: ...


class PendingArrestReportService:
    """Fetch and classify reports without retaining their narrative."""

    def __init__(
        self,
        client: SarpClient,
        review_lookup: PendingArrestReviewLookup | None = None,
    ) -> None:
        self.client = client
        self.review_lookup = review_lookup

    async def scan(
        self,
        *,
        faction_id: int,
        date_from: date,
        date_to: date,
        max_records: int = DEFAULT_MAX_RECORDS,
        now: datetime | None = None,
        guild_id: int | None = None,
        report_channel_ids: Collection[int] | None = None,
        include_unapproved_reviews: bool = False,
    ) -> PendingArrestReportScan:
        if faction_id <= 0:
            raise ValueError("faction_id must be positive")
        if date_from > date_to:
            raise ValueError("date_from cannot be after date_to")
        if max_records < 1:
            raise ValueError("max_records must be positive")
        current = now or datetime.now(UTC)
        current = (
            current.replace(tzinfo=UTC)
            if current.tzinfo is None
            else current.astimezone(UTC)
        )

        reports: list[SarpFactionArrestSummary] = []
        page = 1
        total = 0
        while True:
            payload = await self.client.search_faction_arrests(
                faction_id,
                date_from=date_from,
                # SARP treats dateTo as an exclusive boundary.
                date_to=date_to + timedelta(days=1),
                page=page,
                limit=PAGE_SIZE,
                sort_field="created_at",
                sort_order="desc",
            )
            total = payload.total
            if total > max_records:
                raise PendingReportScanTooLargeError(total, max_records)
            reports.extend(payload.data)
            if len(reports) >= total or not payload.data:
                break
            page += 1

        pending = tuple(
            sorted(
                (
                    classified
                    for report in reports
                    if (classified := _classify_report(report, now=current)) is not None
                ),
                key=lambda item: (item.created_at, item.report_id),
            )
        )
        unapproved = (
            await self._unapproved_reports(
                reports,
                guild_id=guild_id,
                report_channel_ids=report_channel_ids,
            )
            if include_unapproved_reviews
            else ()
        )
        return PendingArrestReportScan(
            faction_id=faction_id,
            date_from=date_from,
            date_to=date_to,
            total_reports=total,
            pending_reports=pending,
            unapproved_reviews_included=include_unapproved_reviews,
            unapproved_reports=unapproved,
        )

    async def _unapproved_reports(
        self,
        reports: Collection[SarpFactionArrestSummary],
        *,
        guild_id: int | None,
        report_channel_ids: Collection[int] | None,
    ) -> tuple[UnapprovedArrestReport, ...]:
        if self.review_lookup is None or guild_id is None:
            return ()
        reviews = await self.review_lookup.latest_published_full_reviews(
            guild_id=guild_id,
            report_ids=tuple(report.id for report in reports),
            channel_ids=report_channel_ids,
        )
        reports_without_reviews = tuple(
            report.id for report in reports if report.id not in reviews
        )
        references = (
            await self.review_lookup.latest_report_references(
                guild_id=guild_id,
                report_ids=reports_without_reviews,
                channel_ids=report_channel_ids,
            )
            if reports_without_reviews
            else {}
        )
        unapproved = []
        for report in reports:
            review = reviews.get(report.id)
            if review is not None and review.verdict is ReviewVerdict.APPROVED:
                continue
            reference = references.get(report.id)
            unapproved.append(
                UnapprovedArrestReport(
                    report_id=report.id,
                    officer_name=_display_name(report.police_name),
                    suspect_name=_display_name(report.suspect_name),
                    created_at=report.created_at.astimezone(UTC),
                    review_verdict=review.verdict if review is not None else None,
                    review_url=(
                        review.jump_url
                        if review is not None
                        else reference.jump_url if reference is not None else None
                    ),
                )
            )
        return tuple(sorted(unapproved, key=lambda item: (item.created_at, item.report_id)))


def unfinished_reason(description: str) -> PendingReportReason | None:
    """Return why a narrative looks unfinished, or ``None`` when it looks complete."""

    normalized = _normalize_description(description)
    if not normalized:
        return PendingReportReason.EMPTY
    if STANDARD_REPORT_PREFIX.match(normalized):
        return None
    if PLACEHOLDER_ONLY.fullmatch(normalized):
        return PendingReportReason.PLACEHOLDER
    if REPEATED_CHARACTER.search(normalized) or _has_repeated_lines(normalized):
        return PendingReportReason.REPEATED_CONTENT
    if len(normalized) < MAX_SHORT_NONSTANDARD_CHARACTERS:
        return PendingReportReason.SHORT_NONSTANDARD
    return None


def status_for_age(created_at: datetime, *, now: datetime) -> PendingReportStatus:
    """Classify an unfinished report against the 18, 24 and 48 hour thresholds."""

    age = max(timedelta(), now - created_at)
    if age < timedelta(hours=18):
        return PendingReportStatus.ON_TIME
    if age < timedelta(hours=24):
        return PendingReportStatus.NEAR_DEADLINE
    if age < timedelta(hours=48):
        return PendingReportStatus.OVER_NORMAL_LIMIT
    return PendingReportStatus.OVER_MAXIMUM_LIMIT


def _classify_report(
    report: SarpFactionArrestSummary,
    *,
    now: datetime,
) -> PendingArrestReport | None:
    reason = unfinished_reason(report.description)
    if reason is None:
        return None
    created_at = report.created_at.astimezone(UTC)
    return PendingArrestReport(
        report_id=report.id,
        officer_name=_display_name(report.police_name),
        suspect_name=_display_name(report.suspect_name),
        created_at=created_at,
        status=status_for_age(created_at, now=now),
        reason=reason,
    )


def _normalize_description(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    return re.sub(r"[ \t\r\f\v]+", " ", normalized)


def _has_repeated_lines(value: str) -> bool:
    lines = [re.sub(r"\W+", " ", line.casefold()).strip() for line in value.splitlines()]
    meaningful = [line for line in lines if line]
    return len(meaningful) >= 2 and len(set(meaningful)) < len(meaningful)


def _display_name(value: str) -> str:
    return " ".join(value.replace("_", " ").split())
