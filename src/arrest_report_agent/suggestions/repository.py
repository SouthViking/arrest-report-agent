"""Transactional PostgreSQL repository for suggestions and their audit history."""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from arrest_report_agent.infrastructure.postgres import PostgresClient
from arrest_report_agent.infrastructure.postgres.schema import (
    suggestion_counters,
    suggestion_events,
    suggestions,
)

from .models import (
    Suggestion,
    SuggestionCategory,
    SuggestionCooldownError,
    SuggestionEvent,
    SuggestionEventType,
    SuggestionLimitReachedError,
    SuggestionNotFoundError,
    SuggestionStatus,
)

MAX_PENDING_SUGGESTIONS = 3
SUBMISSION_COOLDOWN_SECONDS = 60


class SuggestionRepository:
    """Keep quotas, counters and state transitions atomic and guild-scoped."""

    def __init__(self, client: PostgresClient) -> None:
        self.client = client

    async def submit(
        self,
        *,
        guild_id: int,
        author_user_id: int,
        source_channel_id: int,
        category: SuggestionCategory,
        title: str,
        description: str,
        now: datetime | None = None,
    ) -> Suggestion:
        normalized_title = title.strip()
        normalized_description = description.strip()
        if not 1 <= len(normalized_title) <= 100:
            raise ValueError("Suggestion title must contain between 1 and 100 characters")
        if not 1 <= len(normalized_description) <= 1500:
            raise ValueError("Suggestion description must contain between 1 and 1500 characters")
        created_at = now or datetime.now(UTC)
        async with self.client.transaction() as connection:
            await self._lock_submitter(connection, guild_id, author_user_id)
            latest = await connection.scalar(
                sa.select(sa.func.max(suggestions.c.created_at)).where(
                    suggestions.c.guild_id == guild_id,
                    suggestions.c.author_user_id == author_user_id,
                )
            )
            if latest is not None:
                available_at = latest + timedelta(seconds=SUBMISSION_COOLDOWN_SECONDS)
                if created_at < available_at:
                    remaining = max(1, math.ceil((available_at - created_at).total_seconds()))
                    raise SuggestionCooldownError(remaining)
            pending_count = int(
                await connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(suggestions)
                    .where(
                        suggestions.c.guild_id == guild_id,
                        suggestions.c.author_user_id == author_user_id,
                        suggestions.c.status == SuggestionStatus.PENDING.value,
                    )
                )
                or 0
            )
            if pending_count >= MAX_PENDING_SUGGESTIONS:
                raise SuggestionLimitReachedError(MAX_PENDING_SUGGESTIONS)
            number = int(
                (
                    await connection.execute(
                        pg_insert(suggestion_counters)
                        .values(guild_id=guild_id, last_number=1)
                        .on_conflict_do_update(
                            index_elements=[suggestion_counters.c.guild_id],
                            set_={
                                "last_number": suggestion_counters.c.last_number + 1,
                                "updated_at": sa.func.now(),
                            },
                        )
                        .returning(suggestion_counters.c.last_number)
                    )
                ).scalar_one()
            )
            suggestion_id = uuid.uuid4()
            row = (
                (
                    await connection.execute(
                        suggestions.insert()
                        .values(
                            id=suggestion_id,
                            guild_id=guild_id,
                            number=number,
                            author_user_id=author_user_id,
                            source_channel_id=source_channel_id,
                            category_key=category.value,
                            title=normalized_title,
                            description=normalized_description,
                            status=SuggestionStatus.PENDING.value,
                            version=1,
                            created_at=created_at,
                            updated_at=created_at,
                        )
                        .returning(*suggestions.c)
                    )
                )
                .mappings()
                .one()
            )
            await connection.execute(
                suggestion_events.insert().values(
                    id=uuid.uuid4(),
                    suggestion_id=suggestion_id,
                    event_type=SuggestionEventType.SUBMITTED.value,
                    actor_user_id=author_user_id,
                    from_status=None,
                    to_status=SuggestionStatus.PENDING.value,
                    created_at=created_at,
                )
            )
        return self._from_row(row)

    async def list_for_guild(
        self,
        guild_id: int,
        *,
        offset: int,
        limit: int,
    ) -> tuple[tuple[Suggestion, ...], int]:
        if offset < 0 or not 1 <= limit <= 25:
            raise ValueError("Suggestion pagination is outside the supported range")
        async with self.client.connection() as connection:
            total = int(
                await connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(suggestions)
                    .where(suggestions.c.guild_id == guild_id)
                )
                or 0
            )
            rows = (
                (
                    await connection.execute(
                        sa.select(suggestions)
                        .where(suggestions.c.guild_id == guild_id)
                        .order_by(suggestions.c.created_at.desc(), suggestions.c.number.desc())
                        .offset(offset)
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
        return tuple(self._from_row(row) for row in rows), total

    async def get_by_number(self, guild_id: int, number: int) -> Suggestion | None:
        row = await self.client.fetch_one(
            sa.select(suggestions).where(
                suggestions.c.guild_id == guild_id,
                suggestions.c.number == number,
            )
        )
        return self._from_row(row) if row is not None else None

    async def change_status(
        self,
        *,
        guild_id: int,
        number: int,
        status: SuggestionStatus,
        actor_user_id: int,
        now: datetime | None = None,
    ) -> tuple[Suggestion, bool]:
        changed_at = now or datetime.now(UTC)
        async with self.client.transaction() as connection:
            existing = (
                (
                    await connection.execute(
                        sa.select(suggestions).where(
                            suggestions.c.guild_id == guild_id,
                            suggestions.c.number == number,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is None:
                raise SuggestionNotFoundError(f"Suggestion SUG-{number:05d} was not found")
            await self._lock_submitter(connection, guild_id, int(existing["author_user_id"]))
            locked = (
                (
                    await connection.execute(
                        sa.select(suggestions)
                        .where(
                            suggestions.c.guild_id == guild_id,
                            suggestions.c.number == number,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            current = SuggestionStatus(str(locked["status"]))
            if current is status:
                return self._from_row(locked), False
            if status is SuggestionStatus.PENDING:
                pending_count = int(
                    await connection.scalar(
                        sa.select(sa.func.count())
                        .select_from(suggestions)
                        .where(
                            suggestions.c.guild_id == guild_id,
                            suggestions.c.author_user_id == int(locked["author_user_id"]),
                            suggestions.c.status == SuggestionStatus.PENDING.value,
                        )
                    )
                    or 0
                )
                if pending_count >= MAX_PENDING_SUGGESTIONS:
                    raise SuggestionLimitReachedError(MAX_PENDING_SUGGESTIONS)
            updated = (
                (
                    await connection.execute(
                        suggestions.update()
                        .where(suggestions.c.id == locked["id"])
                        .values(
                            status=status.value,
                            version=suggestions.c.version + 1,
                            updated_at=changed_at,
                            archived_at=(changed_at if status is SuggestionStatus.ARCHIVED else None),
                        )
                        .returning(*suggestions.c)
                    )
                )
                .mappings()
                .one()
            )
            await connection.execute(
                suggestion_events.insert().values(
                    id=uuid.uuid4(),
                    suggestion_id=locked["id"],
                    event_type=SuggestionEventType.STATUS_CHANGED.value,
                    actor_user_id=actor_user_id,
                    from_status=current.value,
                    to_status=status.value,
                    created_at=changed_at,
                )
            )
        return self._from_row(updated), True

    async def list_events(self, suggestion_id: uuid.UUID) -> tuple[SuggestionEvent, ...]:
        rows = await self.client.fetch_all(
            sa.select(suggestion_events)
            .where(suggestion_events.c.suggestion_id == suggestion_id)
            .order_by(suggestion_events.c.created_at, suggestion_events.c.id)
        )
        return tuple(
            SuggestionEvent(
                id=row["id"],
                suggestion_id=row["suggestion_id"],
                event_type=SuggestionEventType(str(row["event_type"])),
                actor_user_id=int(row["actor_user_id"]),
                from_status=(
                    SuggestionStatus(str(row["from_status"])) if row["from_status"] is not None else None
                ),
                to_status=SuggestionStatus(str(row["to_status"])),
                comment=str(row["comment"]) if row["comment"] is not None else None,
                created_at=row["created_at"],
            )
            for row in rows
        )

    @staticmethod
    async def _lock_submitter(
        connection: AsyncConnection,
        guild_id: int,
        user_id: int,
    ) -> None:
        await connection.execute(
            sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"suggestions:{guild_id}:{user_id}"},
        )

    @staticmethod
    def _from_row(row: sa.RowMapping) -> Suggestion:
        return Suggestion(
            id=row["id"],
            guild_id=int(row["guild_id"]),
            number=int(row["number"]),
            author_user_id=int(row["author_user_id"]),
            source_channel_id=int(row["source_channel_id"]),
            category=SuggestionCategory(str(row["category_key"])),
            title=str(row["title"]),
            description=str(row["description"]),
            status=SuggestionStatus(str(row["status"])),
            version=int(row["version"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            archived_at=row["archived_at"],
        )
