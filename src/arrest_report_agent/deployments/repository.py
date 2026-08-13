"""PostgreSQL event store and durable Discord delivery outbox."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from arrest_report_agent.infrastructure.postgres.client import PostgresClient
from arrest_report_agent.infrastructure.postgres.schema import (
    deployment_events,
    deployment_notification_deliveries,
)

from .models import DeploymentEventType, RailwayDeploymentEvent


@dataclass(frozen=True, slots=True)
class DeploymentEventRecord:
    id: UUID
    event_type: DeploymentEventType
    deployment_id: str
    project_id: str
    environment_id: str
    service_id: str
    source: str | None
    branch: str | None
    commit_hash: str | None
    commit_message: str | None
    severity: str | None
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class DeploymentDeliveryTarget:
    guild_id: int
    channel_id: int
    kind: str
    notification_key: str


@dataclass(frozen=True, slots=True)
class DeploymentDeliveryRecord:
    id: UUID
    guild_id: int
    channel_id: int
    kind: str
    attempts: int
    event: DeploymentEventRecord


class DeploymentRepository:
    """Persist normalized Railway events and idempotent Discord deliveries."""

    def __init__(self, client: PostgresClient) -> None:
        self.client = client

    async def record_event(
        self,
        event: RailwayDeploymentEvent,
    ) -> tuple[DeploymentEventRecord, bool]:
        event_id = uuid.uuid4()
        statement = (
            pg_insert(deployment_events)
            .values(
                id=event_id,
                provider_event_key=event.provider_event_key,
                event_type=event.event_type.value,
                deployment_id=event.deployment_id,
                project_id=event.resource.project.id,
                environment_id=event.resource.environment.id,
                service_id=event.resource.service.id,
                source=event.details.source,
                branch=event.details.branch,
                commit_hash=event.details.commit_hash,
                commit_message=event.details.commit_message,
                severity=event.severity,
                event_timestamp=event.timestamp,
            )
            .on_conflict_do_nothing(index_elements=[deployment_events.c.provider_event_key])
            .returning(deployment_events.c.id)
        )
        async with self.client.transaction() as connection:
            inserted = (await connection.execute(statement)).scalar_one_or_none()
            row = (
                (
                    await connection.execute(
                        sa.select(deployment_events).where(
                            deployment_events.c.provider_event_key == event.provider_event_key
                        )
                    )
                )
                .mappings()
                .one()
            )
        return self._event_from_row(row), inserted is not None

    async def pending_events(self, *, limit: int = 50) -> tuple[DeploymentEventRecord, ...]:
        rows = await self.client.fetch_all(
            sa.select(deployment_events)
            .where(deployment_events.c.status == "received")
            .order_by(deployment_events.c.created_at)
            .limit(limit)
        )
        return tuple(self._event_from_row(row) for row in rows)

    async def enqueue_deliveries(
        self,
        event_id: UUID,
        targets: tuple[DeploymentDeliveryTarget, ...],
    ) -> int:
        if not targets:
            return 0
        values = [
            {
                "id": uuid.uuid4(),
                "event_id": event_id,
                "guild_id": target.guild_id,
                "channel_id": target.channel_id,
                "notification_kind": target.kind,
                "notification_key": target.notification_key,
            }
            for target in targets
        ]
        async with self.client.transaction() as connection:
            result = await connection.execute(
                pg_insert(deployment_notification_deliveries)
                .values(values)
                .on_conflict_do_nothing(
                    index_elements=[deployment_notification_deliveries.c.notification_key]
                )
            )
        return int(result.rowcount or 0)

    async def mark_event_processed(self, event_id: UUID) -> None:
        await self.client.execute(
            deployment_events.update()
            .where(deployment_events.c.id == event_id)
            .values(status="processed", processed_at=datetime.now(UTC), ignored_reason=None)
        )

    async def mark_event_ignored(self, event_id: UUID, reason: str) -> None:
        await self.client.execute(
            deployment_events.update()
            .where(deployment_events.c.id == event_id)
            .values(
                status="ignored",
                ignored_reason=reason[:100],
                processed_at=datetime.now(UTC),
            )
        )

    async def due_deliveries(self, *, limit: int = 50) -> tuple[DeploymentDeliveryRecord, ...]:
        rows = await self.client.fetch_all(
            sa.select(
                *deployment_notification_deliveries.c,
                *[
                    column.label(f"event_record_{column.name}")
                    for column in deployment_events.c
                ],
            )
            .select_from(
                deployment_notification_deliveries.join(
                    deployment_events,
                    deployment_notification_deliveries.c.event_id == deployment_events.c.id,
                )
            )
            .where(
                deployment_notification_deliveries.c.status.in_(("pending", "retry")),
                deployment_notification_deliveries.c.next_attempt_at <= sa.func.now(),
            )
            .order_by(deployment_notification_deliveries.c.created_at)
            .limit(limit)
        )
        values: list[DeploymentDeliveryRecord] = []
        for row in rows:
            event_row = {
                column.name: row[f"event_record_{column.name}"]
                for column in deployment_events.c
            }
            values.append(
                DeploymentDeliveryRecord(
                    id=UUID(str(row["id"])),
                    guild_id=int(row["guild_id"]),
                    channel_id=int(row["channel_id"]),
                    kind=str(row["notification_kind"]),
                    attempts=int(row["attempts"]),
                    event=self._event_from_row(event_row),
                )
            )
        return tuple(values)

    async def mark_delivery_sent(self, delivery_id: UUID, message_id: int) -> None:
        now = datetime.now(UTC)
        await self.client.execute(
            deployment_notification_deliveries.update()
            .where(deployment_notification_deliveries.c.id == delivery_id)
            .values(
                status="sent",
                discord_message_id=message_id,
                attempts=deployment_notification_deliveries.c.attempts + 1,
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
            deployment_notification_deliveries.update()
            .where(deployment_notification_deliveries.c.id == delivery_id)
            .values(
                status="failed" if terminal else "retry",
                attempts=next_attempt,
                next_attempt_at=datetime.now(UTC) + timedelta(seconds=backoff_seconds),
                last_error=error[:500],
                updated_at=datetime.now(UTC),
            )
        )

    @staticmethod
    def _event_from_row(row: sa.RowMapping | dict[str, object]) -> DeploymentEventRecord:
        return DeploymentEventRecord(
            id=UUID(str(row["id"])),
            event_type=DeploymentEventType(str(row["event_type"])),
            deployment_id=str(row["deployment_id"]),
            project_id=str(row["project_id"]),
            environment_id=str(row["environment_id"]),
            service_id=str(row["service_id"]),
            source=str(row["source"]) if row["source"] is not None else None,
            branch=str(row["branch"]) if row["branch"] is not None else None,
            commit_hash=str(row["commit_hash"]) if row["commit_hash"] is not None else None,
            commit_message=(
                str(row["commit_message"]) if row["commit_message"] is not None else None
            ),
            severity=str(row["severity"]) if row["severity"] is not None else None,
            timestamp=row["event_timestamp"],  # type: ignore[arg-type]
        )
