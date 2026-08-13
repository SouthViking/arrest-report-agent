"""PostgreSQL inbox and durable outbox for GitHub Actions failures."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from arrest_report_agent.infrastructure.postgres.client import PostgresClient
from arrest_report_agent.infrastructure.postgres.schema import (
    ci_notification_deliveries,
    ci_workflow_events,
)

from .models import GitHubWorkflowRunEvent


@dataclass(frozen=True, slots=True)
class CiWorkflowEventRecord:
    id: UUID
    repository_id: int
    repository_full_name: str
    workflow_id: int
    workflow_name: str
    run_id: int
    run_attempt: int
    triggering_event: str
    branch: str | None
    commit_hash: str
    commit_message: str | None
    actor_login: str
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class CiDeliveryTarget:
    guild_id: int
    channel_id: int
    notification_key: str


@dataclass(frozen=True, slots=True)
class CiDeliveryRecord:
    id: UUID
    guild_id: int
    channel_id: int
    attempts: int
    event: CiWorkflowEventRecord


class CiNotificationRepository:
    """Persist workflow failures and exactly-once Discord delivery intents."""

    def __init__(self, client: PostgresClient) -> None:
        self.client = client

    async def record_event(
        self,
        delivery_id: str,
        event: GitHubWorkflowRunEvent,
    ) -> tuple[CiWorkflowEventRecord, bool]:
        run = event.workflow_run
        statement = (
            pg_insert(ci_workflow_events)
            .values(
                id=uuid.uuid4(),
                provider_event_key=event.provider_event_key,
                delivery_id=delivery_id,
                repository_id=event.repository.id,
                repository_full_name=event.repository.full_name,
                workflow_id=run.workflow_id,
                workflow_name=run.name,
                run_id=run.id,
                run_attempt=run.run_attempt,
                triggering_event=run.event,
                branch=run.head_branch,
                commit_hash=run.head_sha,
                commit_message=run.head_commit.message if run.head_commit else None,
                actor_login=run.actor.login,
                event_timestamp=run.updated_at,
            )
            .on_conflict_do_nothing(index_elements=[ci_workflow_events.c.provider_event_key])
            .returning(ci_workflow_events.c.id)
        )
        async with self.client.transaction() as connection:
            inserted = (await connection.execute(statement)).scalar_one_or_none()
            row = (
                (
                    await connection.execute(
                        sa.select(ci_workflow_events).where(
                            ci_workflow_events.c.provider_event_key == event.provider_event_key
                        )
                    )
                )
                .mappings()
                .one()
            )
        return self._event_from_row(row), inserted is not None

    async def pending_events(self, *, limit: int = 50) -> tuple[CiWorkflowEventRecord, ...]:
        rows = await self.client.fetch_all(
            sa.select(ci_workflow_events)
            .where(ci_workflow_events.c.status == "received")
            .order_by(ci_workflow_events.c.created_at)
            .limit(limit)
        )
        return tuple(self._event_from_row(row) for row in rows)

    async def enqueue_deliveries(
        self,
        event_id: UUID,
        targets: tuple[CiDeliveryTarget, ...],
    ) -> int:
        if not targets:
            return 0
        values = [
            {
                "id": uuid.uuid4(),
                "event_id": event_id,
                "guild_id": target.guild_id,
                "channel_id": target.channel_id,
                "notification_key": target.notification_key,
            }
            for target in targets
        ]
        async with self.client.transaction() as connection:
            result = await connection.execute(
                pg_insert(ci_notification_deliveries)
                .values(values)
                .on_conflict_do_nothing(
                    index_elements=[ci_notification_deliveries.c.notification_key]
                )
            )
        return int(result.rowcount or 0)

    async def mark_event_processed(self, event_id: UUID) -> None:
        await self.client.execute(
            ci_workflow_events.update()
            .where(ci_workflow_events.c.id == event_id)
            .values(status="processed", processed_at=datetime.now(UTC), ignored_reason=None)
        )

    async def mark_event_ignored(self, event_id: UUID, reason: str) -> None:
        await self.client.execute(
            ci_workflow_events.update()
            .where(ci_workflow_events.c.id == event_id)
            .values(
                status="ignored",
                ignored_reason=reason[:100],
                processed_at=datetime.now(UTC),
            )
        )

    async def due_deliveries(self, *, limit: int = 50) -> tuple[CiDeliveryRecord, ...]:
        rows = await self.client.fetch_all(
            sa.select(
                *ci_notification_deliveries.c,
                *[
                    column.label(f"event_record_{column.name}")
                    for column in ci_workflow_events.c
                ],
            )
            .select_from(
                ci_notification_deliveries.join(
                    ci_workflow_events,
                    ci_notification_deliveries.c.event_id == ci_workflow_events.c.id,
                )
            )
            .where(
                ci_notification_deliveries.c.status.in_(("pending", "retry")),
                ci_notification_deliveries.c.next_attempt_at <= sa.func.now(),
            )
            .order_by(ci_notification_deliveries.c.created_at)
            .limit(limit)
        )
        return tuple(
            CiDeliveryRecord(
                id=UUID(str(row["id"])),
                guild_id=int(row["guild_id"]),
                channel_id=int(row["channel_id"]),
                attempts=int(row["attempts"]),
                event=self._event_from_row(
                    {
                        column.name: row[f"event_record_{column.name}"]
                        for column in ci_workflow_events.c
                    }
                ),
            )
            for row in rows
        )

    async def mark_delivery_sent(self, delivery_id: UUID, message_id: int) -> None:
        now = datetime.now(UTC)
        await self.client.execute(
            ci_notification_deliveries.update()
            .where(ci_notification_deliveries.c.id == delivery_id)
            .values(
                status="sent",
                discord_message_id=message_id,
                attempts=ci_notification_deliveries.c.attempts + 1,
                last_error=None,
                updated_at=now,
                sent_at=now,
            )
        )

    async def mark_delivery_failed(
        self,
        delivery_id: UUID,
        *,
        attempts: int,
        error: str,
        retryable: bool,
    ) -> None:
        next_attempt = attempts + 1
        terminal = not retryable or next_attempt >= 8
        backoff_seconds = min(5 * 3 ** max(next_attempt - 1, 0), 1800)
        await self.client.execute(
            ci_notification_deliveries.update()
            .where(ci_notification_deliveries.c.id == delivery_id)
            .values(
                status="failed" if terminal else "retry",
                attempts=next_attempt,
                next_attempt_at=datetime.now(UTC) + timedelta(seconds=backoff_seconds),
                last_error=error[:500],
                updated_at=datetime.now(UTC),
            )
        )

    @staticmethod
    def _event_from_row(row: sa.RowMapping | dict[str, object]) -> CiWorkflowEventRecord:
        return CiWorkflowEventRecord(
            id=UUID(str(row["id"])),
            repository_id=cast(int, row["repository_id"]),
            repository_full_name=str(row["repository_full_name"]),
            workflow_id=cast(int, row["workflow_id"]),
            workflow_name=str(row["workflow_name"]),
            run_id=cast(int, row["run_id"]),
            run_attempt=cast(int, row["run_attempt"]),
            triggering_event=str(row["triggering_event"]),
            branch=str(row["branch"]) if row["branch"] is not None else None,
            commit_hash=str(row["commit_hash"]),
            commit_message=(
                str(row["commit_message"]) if row["commit_message"] is not None else None
            ),
            actor_login=str(row["actor_login"]),
            timestamp=row["event_timestamp"],  # type: ignore[arg-type]
        )
