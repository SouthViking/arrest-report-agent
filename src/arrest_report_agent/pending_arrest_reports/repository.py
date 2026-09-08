"""PostgreSQL lookup for formal reviews linked to SARP arrest reports."""

from __future__ import annotations

from collections.abc import Collection

import sqlalchemy as sa

from arrest_report_agent.domain import ReviewScope, ReviewVerdict
from arrest_report_agent.infrastructure.postgres import PostgresClient
from arrest_report_agent.infrastructure.postgres.schema import (
    report_arrest_bindings,
    report_reviews,
)
from arrest_report_agent.review_analytics import ReviewRecordingStatus

from .models import ArrestReportDiscordReference, PublishedArrestReview


class PendingArrestReviewRepository:
    """Resolve the latest published full review for a bounded set of SARP IDs."""

    def __init__(self, client: PostgresClient) -> None:
        self.client = client

    async def latest_published_full_reviews(
        self,
        *,
        guild_id: int,
        report_ids: Collection[int],
        channel_ids: Collection[int] | None,
    ) -> dict[int, PublishedArrestReview]:
        normalized_report_ids = tuple(sorted(set(report_ids)))
        if not normalized_report_ids:
            return {}

        published_at = sa.func.coalesce(
            report_reviews.c.published_at,
            report_reviews.c.created_at,
        )
        statement = (
            sa.select(
                report_arrest_bindings.c.sarp_arrest_id.label("report_id"),
                report_reviews.c.verdict,
                report_reviews.c.guild_id,
                report_reviews.c.parent_channel_id,
                report_reviews.c.root_message_id,
                published_at.label("published_at"),
            )
            .select_from(
                report_arrest_bindings.join(
                    report_reviews,
                    sa.and_(
                        report_arrest_bindings.c.guild_id == report_reviews.c.guild_id,
                        report_arrest_bindings.c.root_message_id
                        == report_reviews.c.root_message_id,
                    ),
                )
            )
            .where(
                report_arrest_bindings.c.guild_id == guild_id,
                report_arrest_bindings.c.sarp_arrest_id.in_(normalized_report_ids),
                report_reviews.c.status == ReviewRecordingStatus.PUBLISHED.value,
                report_reviews.c.review_scope == ReviewScope.FULL.value,
            )
            .order_by(
                report_arrest_bindings.c.sarp_arrest_id,
                published_at.desc(),
                report_reviews.c.attempt_number.desc(),
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
        reviews: dict[int, PublishedArrestReview] = {}
        for row in rows:
            report_id = int(row["report_id"])
            if report_id in reviews:
                continue
            reviews[report_id] = PublishedArrestReview(
                report_id=report_id,
                verdict=ReviewVerdict(str(row["verdict"])),
                guild_id=int(row["guild_id"]),
                parent_channel_id=int(row["parent_channel_id"]),
                root_message_id=int(row["root_message_id"]),
                published_at=row["published_at"],
            )
        return reviews

    async def latest_report_references(
        self,
        *,
        guild_id: int,
        report_ids: Collection[int],
        channel_ids: Collection[int] | None,
    ) -> dict[int, ArrestReportDiscordReference]:
        """Resolve the newest Discord root even when no full review was published."""

        normalized_report_ids = tuple(sorted(set(report_ids)))
        if not normalized_report_ids:
            return {}

        recorded_at = sa.func.coalesce(
            report_arrest_bindings.c.updated_at,
            report_arrest_bindings.c.created_at,
        )
        statement = (
            sa.select(
                report_arrest_bindings.c.sarp_arrest_id.label("report_id"),
                report_arrest_bindings.c.guild_id,
                report_arrest_bindings.c.parent_channel_id,
                report_arrest_bindings.c.root_message_id,
                recorded_at.label("recorded_at"),
            )
            .where(
                report_arrest_bindings.c.guild_id == guild_id,
                report_arrest_bindings.c.sarp_arrest_id.in_(normalized_report_ids),
            )
            .order_by(
                report_arrest_bindings.c.sarp_arrest_id,
                recorded_at.desc(),
                report_arrest_bindings.c.root_message_id.desc(),
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
        references: dict[int, ArrestReportDiscordReference] = {}
        for row in rows:
            report_id = int(row["report_id"])
            if report_id in references:
                continue
            references[report_id] = ArrestReportDiscordReference(
                report_id=report_id,
                guild_id=int(row["guild_id"]),
                parent_channel_id=int(row["parent_channel_id"]),
                root_message_id=int(row["root_message_id"]),
                recorded_at=row["recorded_at"],
            )
        return references
