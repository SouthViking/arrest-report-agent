"""PostgreSQL lookup for structured reviews linked to SARP arrest reports."""

from __future__ import annotations

from collections.abc import Collection

import sqlalchemy as sa

from arrest_report_agent.domain import ReviewScope, ReviewVerdict
from arrest_report_agent.infrastructure.postgres import PostgresClient
from arrest_report_agent.infrastructure.postgres.schema import (
    arrest_report_submissions,
    report_arrest_bindings,
    report_reviews,
)
from arrest_report_agent.review_analytics import ReviewRecordingStatus

from .models import ArrestReportReviewReference, ArrestReviewSource


class PendingArrestReviewRepository:
    """Resolve the newest structured review root and its latest full verdict."""

    def __init__(self, client: PostgresClient) -> None:
        self.client = client

    async def latest_reviews(
        self,
        *,
        guild_id: int,
        report_ids: Collection[int],
        channel_ids: Collection[int] | None,
    ) -> dict[int, ArrestReportReviewReference]:
        normalized_report_ids = tuple(sorted(set(report_ids)))
        if not normalized_report_ids:
            return {}

        bound_at = sa.func.coalesce(
            report_arrest_bindings.c.updated_at,
            report_arrest_bindings.c.created_at,
        )
        published_at = sa.func.coalesce(
            report_reviews.c.published_at,
            report_reviews.c.created_at,
        )
        reviewed_at = sa.func.coalesce(published_at, bound_at)
        submission_join = sa.and_(
            report_arrest_bindings.c.guild_id == arrest_report_submissions.c.guild_id,
            report_arrest_bindings.c.root_message_id
            == arrest_report_submissions.c.root_message_id,
        )
        review_join = sa.and_(
            report_arrest_bindings.c.guild_id == report_reviews.c.guild_id,
            report_arrest_bindings.c.root_message_id == report_reviews.c.root_message_id,
            arrest_report_submissions.c.id.is_not(None),
            report_reviews.c.status == ReviewRecordingStatus.PUBLISHED.value,
            report_reviews.c.review_scope == ReviewScope.FULL.value,
        )
        statement = (
            sa.select(
                report_arrest_bindings.c.sarp_arrest_id.label("report_id"),
                report_arrest_bindings.c.guild_id,
                report_arrest_bindings.c.parent_channel_id,
                report_arrest_bindings.c.root_message_id,
                reviewed_at.label("reviewed_at"),
                arrest_report_submissions.c.id.label("structured_submission_id"),
                report_reviews.c.verdict,
            )
            .select_from(
                report_arrest_bindings.outerjoin(
                    arrest_report_submissions,
                    submission_join,
                ).outerjoin(report_reviews, review_join)
            )
            .where(
                report_arrest_bindings.c.guild_id == guild_id,
                report_arrest_bindings.c.sarp_arrest_id.in_(normalized_report_ids),
            )
            .order_by(
                report_arrest_bindings.c.sarp_arrest_id,
                report_arrest_bindings.c.root_message_id.desc(),
                bound_at.desc(),
                published_at.desc().nulls_last(),
                report_reviews.c.attempt_number.desc().nulls_last(),
            )
        )
        normalized_channel_ids = None if channel_ids is None else tuple(sorted(set(channel_ids)))
        if normalized_channel_ids is not None:
            if not normalized_channel_ids:
                return {}
            statement = statement.where(
                report_arrest_bindings.c.parent_channel_id.in_(normalized_channel_ids)
            )

        rows = await self.client.fetch_all(statement)
        reviews: dict[int, ArrestReportReviewReference] = {}
        for row in rows:
            report_id = int(row["report_id"])
            if report_id in reviews:
                continue
            verdict = row["verdict"]
            source = (
                ArrestReviewSource.STRUCTURED
                if row["structured_submission_id"] is not None
                else ArrestReviewSource.LEGACY
            )
            reviews[report_id] = ArrestReportReviewReference(
                report_id=report_id,
                source=source,
                approved=(
                    source is ArrestReviewSource.STRUCTURED
                    and verdict is not None
                    and ReviewVerdict(str(verdict)) is ReviewVerdict.APPROVED
                ),
                guild_id=int(row["guild_id"]),
                parent_channel_id=int(row["parent_channel_id"]),
                root_message_id=int(row["root_message_id"]),
                reviewed_at=row["reviewed_at"],
            )
        return reviews
