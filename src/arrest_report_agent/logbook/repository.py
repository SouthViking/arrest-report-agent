"""Transactional persistence for guild-scoped structured logbook entries."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from arrest_report_agent.infrastructure.postgres import PostgresClient
from arrest_report_agent.infrastructure.postgres.schema import (
    logbook_assets,
    logbook_counters,
    logbook_entries,
)

from .models import (
    CATEGORY_SUBTYPES,
    LogbookAsset,
    LogbookCategory,
    LogbookCategoryCount,
    LogbookEntry,
    LogbookStatus,
    LogbookSubtypeCount,
    LogbookSummary,
    MoneyDirection,
    PendingLogbookAsset,
    available_subtypes,
)


class LogbookRepository:
    """Allocate references and persist publication state atomically."""

    def __init__(self, client: PostgresClient) -> None:
        self.client = client

    async def reserve(
        self,
        *,
        guild_id: int,
        channel_id: int,
        interaction_id: int,
        author_user_id: int,
        category: LogbookCategory,
        subtype: str,
        subject: str,
        description: str,
        money_direction: MoneyDirection | None,
        money_amount: Decimal | None,
        money_detail: str | None,
        occurred_at: datetime | None = None,
    ) -> tuple[LogbookEntry, bool]:
        normalized_subject = subject.strip()
        normalized_description = description.strip()
        if subtype not in dict(available_subtypes(category, money_direction)):
            raise ValueError("Subtype does not match the selected category and money direction")
        if not 1 <= len(normalized_subject) <= 200:
            raise ValueError("Logbook subject must contain between 1 and 200 characters")
        if not 1 <= len(normalized_description) <= 2_000:
            raise ValueError("Logbook description must contain between 1 and 2000 characters")
        if (money_direction is None) is not (money_amount is None):
            raise ValueError("Money direction and amount must be supplied together")
        if money_amount is not None and money_amount <= 0:
            raise ValueError("Money amount must be positive")
        timestamp = occurred_at or datetime.now(UTC)
        async with self.client.transaction() as connection:
            await connection.execute(
                sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"logbook:{interaction_id}"},
            )
            existing = (
                (
                    await connection.execute(
                        sa.select(logbook_entries).where(logbook_entries.c.interaction_id == interaction_id)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                return self._entry_from_row(existing), False
            number = int(
                (
                    await connection.execute(
                        pg_insert(logbook_counters)
                        .values(guild_id=guild_id, last_number=1)
                        .on_conflict_do_update(
                            index_elements=[logbook_counters.c.guild_id],
                            set_={
                                "last_number": logbook_counters.c.last_number + 1,
                                "updated_at": sa.func.now(),
                            },
                        )
                        .returning(logbook_counters.c.last_number)
                    )
                ).scalar_one()
            )
            row = (
                (
                    await connection.execute(
                        logbook_entries.insert()
                        .values(
                            id=uuid.uuid4(),
                            guild_id=guild_id,
                            number=number,
                            channel_id=channel_id,
                            interaction_id=interaction_id,
                            author_user_id=author_user_id,
                            category_key=category.value,
                            subtype_key=subtype,
                            subject=normalized_subject,
                            description=normalized_description,
                            money_direction=(money_direction.value if money_direction is not None else None),
                            money_amount=money_amount,
                            money_detail=money_detail,
                            status=LogbookStatus.PUBLISHING.value,
                            version=1,
                            occurred_at=timestamp,
                            created_at=timestamp,
                            updated_at=timestamp,
                        )
                        .returning(*logbook_entries.c)
                    )
                )
                .mappings()
                .one()
            )
        return self._entry_from_row(row), True

    async def replace_assets(
        self,
        entry_id: uuid.UUID,
        assets: tuple[PendingLogbookAsset, ...],
    ) -> tuple[LogbookAsset, ...]:
        async with self.client.transaction() as connection:
            await connection.execute(logbook_assets.delete().where(logbook_assets.c.entry_id == entry_id))
            rows: list[sa.RowMapping] = []
            for position, asset in enumerate(assets):
                row = (
                    (
                        await connection.execute(
                            logbook_assets.insert()
                            .values(
                                id=uuid.uuid4(),
                                entry_id=entry_id,
                                position=position,
                                **asset.model_dump(),
                            )
                            .returning(*logbook_assets.c)
                        )
                    )
                    .mappings()
                    .one()
                )
                rows.append(row)
        return tuple(self._asset_from_row(row) for row in rows)

    async def mark_published(self, entry_id: uuid.UUID, message_id: int) -> LogbookEntry:
        return await self._transition(
            entry_id,
            status=LogbookStatus.PUBLISHED,
            message_id=message_id,
        )

    async def mark_failed(self, entry_id: uuid.UUID) -> LogbookEntry:
        return await self._transition(entry_id, status=LogbookStatus.FAILED)

    async def summarize(
        self,
        *,
        guild_id: int,
        date_from: date,
        date_to: date,
        category: LogbookCategory | None = None,
        subtype: str | None = None,
    ) -> LogbookSummary:
        """Aggregate published entries without loading their narrative content."""

        if guild_id <= 0:
            raise ValueError("guild_id must be positive")
        if date_from > date_to:
            raise ValueError("date_from must not be after date_to")
        if subtype is not None:
            if category is None:
                raise ValueError("subtype filters require a category")
            if subtype not in dict(CATEGORY_SUBTYPES[category]):
                raise ValueError("subtype does not belong to the selected category")

        range_start = datetime.combine(date_from, time.min, tzinfo=UTC)
        range_end = datetime.combine(date_to, time.min, tzinfo=UTC) + timedelta(days=1)
        filters = [
            logbook_entries.c.guild_id == guild_id,
            logbook_entries.c.status == LogbookStatus.PUBLISHED.value,
            logbook_entries.c.occurred_at >= range_start,
            logbook_entries.c.occurred_at < range_end,
        ]
        if category is not None:
            filters.append(logbook_entries.c.category_key == category.value)
        if subtype is not None:
            filters.append(logbook_entries.c.subtype_key == subtype)

        asset_counts = (
            sa.select(
                logbook_assets.c.entry_id,
                sa.func.count(logbook_assets.c.id).label("asset_count"),
            )
            .group_by(logbook_assets.c.entry_id)
            .subquery()
        )
        metrics_statement = (
            sa.select(
                sa.func.count(logbook_entries.c.id).label("total_entries"),
                sa.func.coalesce(sa.func.sum(asset_counts.c.asset_count), 0).label(
                    "attachment_count"
                ),
                sa.func.coalesce(
                    sa.func.sum(
                        sa.case(
                            (
                                logbook_entries.c.money_direction
                                == MoneyDirection.INCOME.value,
                                logbook_entries.c.money_amount,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("income_total"),
                sa.func.coalesce(
                    sa.func.sum(
                        sa.case(
                            (
                                logbook_entries.c.money_direction
                                == MoneyDirection.WITHDRAWAL.value,
                                logbook_entries.c.money_amount,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("withdrawal_total"),
            )
            .select_from(
                logbook_entries.outerjoin(
                    asset_counts,
                    asset_counts.c.entry_id == logbook_entries.c.id,
                )
            )
            .where(*filters)
        )
        distribution_statement = (
            sa.select(
                logbook_entries.c.category_key,
                logbook_entries.c.subtype_key,
                sa.func.count(logbook_entries.c.id).label("entry_count"),
            )
            .where(*filters)
            .group_by(logbook_entries.c.category_key, logbook_entries.c.subtype_key)
            .order_by(
                sa.func.count(logbook_entries.c.id).desc(),
                logbook_entries.c.category_key,
                logbook_entries.c.subtype_key,
            )
        )
        async with self.client.connection() as connection:
            metrics_result = await connection.execute(metrics_statement)
            metrics = metrics_result.mappings().one()
            distribution_result = await connection.execute(distribution_statement)
            distribution = distribution_result.mappings().all()

        category_totals: dict[LogbookCategory, int] = {}
        subtype_totals: list[LogbookSubtypeCount] = []
        for row in distribution:
            row_category = LogbookCategory(str(row["category_key"]))
            count = int(row["entry_count"])
            category_totals[row_category] = category_totals.get(row_category, 0) + count
            subtype_totals.append(
                LogbookSubtypeCount(
                    category=row_category,
                    subtype=str(row["subtype_key"]),
                    count=count,
                )
            )
        categories = tuple(
            LogbookCategoryCount(category=item, count=count)
            for item, count in sorted(
                category_totals.items(),
                key=lambda pair: (-pair[1], pair[0].value),
            )
        )
        subtypes = tuple(
            sorted(
                subtype_totals,
                key=lambda item: (-item.count, item.category.value, item.subtype),
            )
        )
        return LogbookSummary(
            guild_id=guild_id,
            date_from=date_from,
            date_to=date_to,
            category_filter=category,
            subtype_filter=subtype,
            total_entries=int(metrics["total_entries"] or 0),
            attachment_count=int(metrics["attachment_count"] or 0),
            income_total=Decimal(metrics["income_total"] or 0),
            withdrawal_total=Decimal(metrics["withdrawal_total"] or 0),
            categories=categories,
            subtypes=subtypes,
        )

    async def _transition(
        self,
        entry_id: uuid.UUID,
        *,
        status: LogbookStatus,
        message_id: int | None = None,
    ) -> LogbookEntry:
        values: dict[str, object] = {
            "status": status.value,
            "updated_at": datetime.now(UTC),
        }
        if message_id is not None:
            values["message_id"] = message_id
        async with self.client.transaction() as connection:
            row = (
                (
                    await connection.execute(
                        logbook_entries.update()
                        .where(logbook_entries.c.id == entry_id)
                        .values(**values)
                        .returning(*logbook_entries.c)
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise LookupError("Logbook entry does not exist")
        return self._entry_from_row(row)

    @staticmethod
    def _entry_from_row(row: sa.RowMapping) -> LogbookEntry:
        return LogbookEntry(
            id=row["id"],
            guild_id=int(row["guild_id"]),
            number=int(row["number"]),
            channel_id=int(row["channel_id"]),
            message_id=int(row["message_id"]) if row["message_id"] is not None else None,
            interaction_id=int(row["interaction_id"]),
            author_user_id=int(row["author_user_id"]),
            category=LogbookCategory(str(row["category_key"])),
            subtype=str(row["subtype_key"]),
            subject=str(row["subject"]),
            description=str(row["description"]),
            money_direction=(
                MoneyDirection(str(row["money_direction"])) if row["money_direction"] is not None else None
            ),
            money_amount=row["money_amount"],
            money_detail=str(row["money_detail"]) if row["money_detail"] is not None else None,
            status=LogbookStatus(str(row["status"])),
            version=int(row["version"]),
            occurred_at=row["occurred_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _asset_from_row(row: sa.RowMapping) -> LogbookAsset:
        return LogbookAsset(
            id=row["id"],
            entry_id=row["entry_id"],
            position=int(row["position"]),
            object_key=str(row["object_key"]),
            sha256=str(row["sha256"]),
            filename=str(row["filename"]),
            content_type=str(row["content_type"]),
            size_bytes=int(row["size_bytes"]),
            created_at=row["created_at"],
        )
