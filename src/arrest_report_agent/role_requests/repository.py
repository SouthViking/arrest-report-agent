"""Transactional persistence for private, mirrored role-request reviews."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from arrest_report_agent.infrastructure.postgres import PostgresClient
from arrest_report_agent.infrastructure.postgres.schema import (
    role_request_review_messages,
    role_requests,
)

from .models import (
    RoleRequestDecision,
    RoleRequestKind,
    RoleRequestReviewMessage,
    RoleRequestShiftChange,
    StoredRoleRequest,
    StoredRoleRequestStatus,
)


class RoleRequestBusyError(RuntimeError):
    """Do not replace a request while it is being published or applying roles."""

    def __init__(self, request: StoredRoleRequest) -> None:
        super().__init__("A role request is being published or resolved")
        self.request = request


class RoleRequestRepository:
    """Keep role-request SQL, idempotency and concurrency outside Discord."""

    _BUSY_STATUSES = (
        StoredRoleRequestStatus.PUBLISHING.value,
        StoredRoleRequestStatus.PROCESSING.value,
    )

    def __init__(self, client: PostgresClient) -> None:
        self.client = client

    async def reserve(
        self,
        *,
        guild_id: int,
        interaction_id: int,
        requester_id: int,
        kind: RoleRequestKind,
        add_role_ids: tuple[int, ...],
        remove_role_ids: tuple[int, ...],
        reason: str | None,
        public_channel_id: int,
        shift_change: RoleRequestShiftChange | None = None,
    ) -> tuple[StoredRoleRequest, bool]:
        now = datetime.now(UTC)
        async with self.client.transaction() as connection:
            await self._lock_requester(connection, guild_id, requester_id)
            existing = await self._one_or_none(
                sa.select(role_requests).where(role_requests.c.interaction_id == interaction_id),
                connection,
            )
            if existing is not None:
                return self._request_from_row(existing), False
            pending = await self._one_or_none(
                sa.select(role_requests)
                .where(
                    role_requests.c.guild_id == guild_id,
                    role_requests.c.requester_id == requester_id,
                    role_requests.c.status.in_(self._BUSY_STATUSES),
                )
                .order_by(role_requests.c.created_at.desc())
                .limit(1),
                connection,
            )
            if pending is not None:
                raise RoleRequestBusyError(self._request_from_row(pending))
            row = (
                (
                    await connection.execute(
                        role_requests.insert()
                        .values(
                            id=uuid.uuid4(),
                            guild_id=guild_id,
                            interaction_id=interaction_id,
                            requester_id=requester_id,
                            kind=kind.value,
                            shift_change=shift_change.model_dump(mode="json") if shift_change else None,
                            add_role_ids=list(add_role_ids),
                            remove_role_ids=list(remove_role_ids),
                            reason=reason,
                            status=StoredRoleRequestStatus.PUBLISHING.value,
                            public_channel_id=public_channel_id,
                            version=1,
                            created_at=now,
                            updated_at=now,
                        )
                        .returning(*role_requests.c)
                    )
                )
                .mappings()
                .one()
            )
        return self._request_from_row(row), True

    async def mark_published(
        self,
        request_id: UUID,
        *,
        public_message_id: int,
        review_messages: tuple[tuple[int, int], ...],
    ) -> StoredRoleRequest:
        if not review_messages:
            raise ValueError("At least one private review message is required")
        now = datetime.now(UTC)
        async with self.client.transaction() as connection:
            draft = await self._locked_request(connection, request_id)
            if draft is None or draft.status is not StoredRoleRequestStatus.PUBLISHING:
                raise LookupError("Role request is not publishing")
            # The requester lock is also acquired by claim_decision: either approval
            # wins before reserve(), or publication prevents approval until it finishes.
            await connection.execute(
                role_requests.update().where(
                    role_requests.c.guild_id == draft.guild_id,
                    role_requests.c.requester_id == draft.requester_id,
                    role_requests.c.status == StoredRoleRequestStatus.PENDING.value,
                ).values(
                    status=StoredRoleRequestStatus.CANCELLED.value,
                    replaced_by_request_id=request_id,
                    cancellation_sync_due=now,
                    decision_comment="Reemplazada por una nueva solicitud.",
                    unapproved_add_role_ids=role_requests.c.add_role_ids,
                    unapproved_remove_role_ids=role_requests.c.remove_role_ids,
                    decided_at=now, updated_at=now, version=role_requests.c.version + 1,
                )
            )
            row = (
                (
                    await connection.execute(
                        role_requests.update()
                        .where(
                            role_requests.c.id == request_id,
                            role_requests.c.status == StoredRoleRequestStatus.PUBLISHING.value,
                        )
                        .values(
                            public_message_id=public_message_id,
                            status=StoredRoleRequestStatus.PENDING.value,
                            version=role_requests.c.version + 1,
                            updated_at=now,
                        )
                        .returning(*role_requests.c)
                    )
                )
                .mappings()
                .one()
            )
            await connection.execute(
                role_request_review_messages.insert(),
                [
                    {
                        "id": uuid.uuid4(),
                        "request_id": request_id,
                        "channel_id": channel_id,
                        "message_id": message_id,
                        "created_at": now,
                        "updated_at": now,
                    }
                    for channel_id, message_id in review_messages
                ],
            )
        return self._request_from_row(row)

    async def mark_failed(self, request_id: UUID) -> None:
        await self.client.execute(
            role_requests.update()
            .where(
                role_requests.c.id == request_id,
                role_requests.c.status.in_(
                    (
                        StoredRoleRequestStatus.PUBLISHING.value,
                        StoredRoleRequestStatus.PROCESSING.value,
                    )
                ),
            )
            .values(
                status=StoredRoleRequestStatus.FAILED.value,
                version=role_requests.c.version + 1,
                updated_at=datetime.now(UTC),
            )
        )

    async def get(self, request_id: UUID) -> StoredRoleRequest | None:
        row = await self.client.fetch_one(sa.select(role_requests).where(role_requests.c.id == request_id))
        return None if row is None else self._request_from_row(row)

    async def get_by_review_message(
        self, guild_id: int, channel_id: int, message_id: int
    ) -> StoredRoleRequest | None:
        row = await self.client.fetch_one(
            sa.select(role_requests)
            .join(
                role_request_review_messages,
                role_request_review_messages.c.request_id == role_requests.c.id,
            )
            .where(
                role_requests.c.guild_id == guild_id,
                role_request_review_messages.c.channel_id == channel_id,
                role_request_review_messages.c.message_id == message_id,
            )
        )
        return None if row is None else self._request_from_row(row)

    async def get_by_public_message(
        self, guild_id: int, channel_id: int, message_id: int
    ) -> StoredRoleRequest | None:
        row = await self.client.fetch_one(
            sa.select(role_requests).where(
                role_requests.c.guild_id == guild_id,
                role_requests.c.public_channel_id == channel_id,
                role_requests.c.public_message_id == message_id,
            )
        )
        return None if row is None else self._request_from_row(row)

    async def review_messages(self, request_id: UUID) -> tuple[RoleRequestReviewMessage, ...]:
        rows = await self.client.fetch_all(
            sa.select(role_request_review_messages)
            .where(role_request_review_messages.c.request_id == request_id)
            .order_by(role_request_review_messages.c.created_at, role_request_review_messages.c.channel_id)
        )
        return tuple(self._review_message_from_row(row) for row in rows)

    async def claim_decision(self, request_id: UUID) -> StoredRoleRequest | None:
        now = datetime.now(UTC)
        async with self.client.transaction() as connection:
            request = await self._locked_request(connection, request_id)
            if request is None or request.status is not StoredRoleRequestStatus.PENDING:
                return None
            publishing = await self._one_or_none(
                sa.select(role_requests.c.id).where(
                    role_requests.c.guild_id == request.guild_id,
                    role_requests.c.requester_id == request.requester_id,
                    role_requests.c.status == StoredRoleRequestStatus.PUBLISHING.value,
                ), connection,
            )
            if publishing is not None:
                return None
            row = (
                (
                    await connection.execute(
                        role_requests.update()
                        .where(
                            role_requests.c.id == request_id,
                            role_requests.c.status == StoredRoleRequestStatus.PENDING.value,
                        )
                        .values(
                            status=StoredRoleRequestStatus.PROCESSING.value,
                            version=role_requests.c.version + 1,
                            updated_at=now,
                        )
                        .returning(*role_requests.c)
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else self._request_from_row(row)

    async def release_decision(self, request_id: UUID) -> None:
        await self.client.execute(
            role_requests.update()
            .where(
                role_requests.c.id == request_id,
                role_requests.c.status == StoredRoleRequestStatus.PROCESSING.value,
            )
            .values(
                status=StoredRoleRequestStatus.PENDING.value,
                version=role_requests.c.version + 1,
                updated_at=datetime.now(UTC),
            )
        )

    async def finalize(
        self,
        request_id: UUID,
        decision: RoleRequestDecision,
    ) -> StoredRoleRequest:
        status = StoredRoleRequestStatus(decision.status.value)
        async with self.client.transaction() as connection:
            row = (
                (
                    await connection.execute(
                        role_requests.update()
                        .where(
                            role_requests.c.id == request_id,
                            role_requests.c.status == StoredRoleRequestStatus.PROCESSING.value,
                        )
                        .values(
                            status=status.value,
                            reviewer_id=decision.reviewer_id,
                            decision_comment=decision.comment,
                            approved_add_role_ids=list(decision.approved_add_role_ids),
                            approved_remove_role_ids=list(decision.approved_remove_role_ids),
                            unapproved_add_role_ids=list(decision.unapproved_add_role_ids),
                            unapproved_remove_role_ids=list(decision.unapproved_remove_role_ids),
                            decided_at=decision.decided_at,
                            version=role_requests.c.version + 1,
                            updated_at=datetime.now(UTC),
                        )
                        .returning(*role_requests.c)
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise LookupError(f"Role request {request_id} is not processing")
        return self._request_from_row(row)

    async def mark_dm_delivery(self, request_id: UUID, delivered: bool) -> None:
        await self.client.execute(
            role_requests.update()
            .where(role_requests.c.id == request_id)
            .values(requester_dm_delivered=delivered, updated_at=datetime.now(UTC))
        )

    async def list_cancellation_sync(
        self, *, replacement_id: UUID | None = None,
    ) -> tuple[StoredRoleRequest, ...]:
        statement = sa.select(role_requests).where(
            role_requests.c.status == StoredRoleRequestStatus.CANCELLED.value,
            role_requests.c.cancellation_sync_due <= datetime.now(UTC),
            role_requests.c.replaced_by_request_id.is_not(None),
        )
        if replacement_id is not None:
            statement = statement.where(role_requests.c.replaced_by_request_id == replacement_id)
        rows = await self.client.fetch_all(
            statement.order_by(role_requests.c.cancellation_sync_due, role_requests.c.id).limit(50)
        )
        return tuple(self._request_from_row(row) for row in rows)

    async def set_cancellation_sync_due(self, request_id: UUID, due: datetime | None) -> None:
        await self.client.execute(role_requests.update().where(
            role_requests.c.id == request_id,
            role_requests.c.status == StoredRoleRequestStatus.CANCELLED.value,
        ).values(cancellation_sync_due=due))

    @staticmethod
    async def _lock_requester(connection: AsyncConnection, guild_id: int, requester_id: int) -> None:
        await connection.execute(
            sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"role-request:{guild_id}:{requester_id}"},
        )

    async def _locked_request(
        self, connection: AsyncConnection, request_id: UUID,
    ) -> StoredRoleRequest | None:
        statement = sa.select(role_requests).where(role_requests.c.id == request_id)
        row = await self._one_or_none(statement, connection)
        if row is None:
            return None
        await self._lock_requester(connection, int(row["guild_id"]), int(row["requester_id"]))
        # Re-read after the lock; another worker may have cancelled/claimed it.
        row = await self._one_or_none(statement, connection)
        return None if row is None else self._request_from_row(row)

    @staticmethod
    async def _one_or_none(statement: sa.Executable, connection: object) -> sa.RowMapping | None:
        result = await connection.execute(statement)  # type: ignore[attr-defined]
        return cast(sa.RowMapping | None, result.mappings().one_or_none())

    @staticmethod
    def _request_from_row(row: sa.RowMapping) -> StoredRoleRequest:
        return StoredRoleRequest(
            id=UUID(str(row["id"])),
            guild_id=int(row["guild_id"]),
            interaction_id=int(row["interaction_id"]),
            requester_id=int(row["requester_id"]),
            kind=RoleRequestKind(str(row["kind"])),
            shift_change=row.get("shift_change"),
            add_role_ids=tuple(int(value) for value in row["add_role_ids"]),
            remove_role_ids=tuple(int(value) for value in row["remove_role_ids"]),
            reason=row["reason"],
            status=StoredRoleRequestStatus(str(row["status"])),
            public_channel_id=int(row["public_channel_id"]),
            public_message_id=(int(row["public_message_id"]) if row["public_message_id"] else None),
            reviewer_id=int(row["reviewer_id"]) if row["reviewer_id"] else None,
            decision_comment=row["decision_comment"],
            approved_add_role_ids=tuple(int(value) for value in row["approved_add_role_ids"] or ()),
            approved_remove_role_ids=tuple(int(value) for value in row["approved_remove_role_ids"] or ()),
            unapproved_add_role_ids=tuple(int(value) for value in row["unapproved_add_role_ids"] or ()),
            unapproved_remove_role_ids=tuple(int(value) for value in row["unapproved_remove_role_ids"] or ()),
            requester_dm_delivered=row["requester_dm_delivered"],
            version=int(row["version"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            decided_at=row["decided_at"],
            replaced_by_request_id=row["replaced_by_request_id"],
            cancellation_sync_due=row["cancellation_sync_due"],
        )

    @staticmethod
    def _review_message_from_row(row: sa.RowMapping) -> RoleRequestReviewMessage:
        return RoleRequestReviewMessage(
            request_id=UUID(str(row["request_id"])),
            channel_id=int(row["channel_id"]),
            message_id=int(row["message_id"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
