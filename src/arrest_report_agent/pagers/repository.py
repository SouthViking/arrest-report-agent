"""PostgreSQL repository for deterministic pagers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from arrest_report_agent.infrastructure.postgres import PostgresClient
from arrest_report_agent.infrastructure.postgres.schema import (
    discord_guilds,
    pager_recipient_roles,
    pager_responders,
    pagers,
)

from .models import Pager, PagerCreatorCannotRespondError, PagerResponder, PagerStatus, UserPager


class PagerRepository:
    """Keep pager SQL and concurrency control outside Discord adapters."""

    def __init__(self, client: PostgresClient) -> None:
        self.client = client

    async def create_publishing(
        self,
        *,
        guild_id: int,
        channel_id: int,
        creator_user_id: int,
        creator_callsign: str,
        recipient_role_ids: tuple[int, ...],
        location: str,
        situation: str,
        starting_number: int,
    ) -> Pager:
        if not recipient_role_ids or len(recipient_role_ids) > 25:
            raise ValueError("A pager requires between 1 and 25 recipient roles")
        if len(recipient_role_ids) != len(set(recipient_role_ids)):
            raise ValueError("Pager recipient roles must be unique")
        async with self.client.transaction() as tx:
            guild = await tx.scalar(
                sa.select(discord_guilds.c.discord_id)
                .where(discord_guilds.c.discord_id == guild_id)
                .with_for_update()
            )
            if guild is None:
                raise LookupError(f"Guild {guild_id} is not configured")
            next_number = int(
                await tx.scalar(
                    sa.select(
                        sa.func.greatest(
                            sa.func.coalesce(sa.func.max(pagers.c.number) + 1, starting_number),
                            starting_number,
                        )
                    ).where(pagers.c.guild_id == guild_id)
                )
                or starting_number
            )
            pager_id = uuid.uuid4()
            await tx.execute(
                pagers.insert().values(
                    id=pager_id,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    number=next_number,
                    creator_user_id=creator_user_id,
                    creator_callsign=creator_callsign,
                    location=location,
                    situation=situation,
                    status=PagerStatus.PUBLISHING.value,
                )
            )
            await tx.execute(
                pager_recipient_roles.insert(),
                [
                    {
                        "id": uuid.uuid4(),
                        "pager_id": pager_id,
                        "role_id": role_id,
                        "position": position,
                    }
                    for position, role_id in enumerate(recipient_role_ids)
                ],
            )
            return await self._get(pager_id, tx)

    async def publish(self, pager_id: UUID, message_id: int) -> Pager:
        async with self.client.transaction() as tx:
            await tx.execute(
                pagers.update()
                .where(pagers.c.id == pager_id, pagers.c.status == PagerStatus.PUBLISHING.value)
                .values(
                    message_id=message_id,
                    status=PagerStatus.ACTIVE.value,
                    updated_at=datetime.now(UTC),
                )
            )
            return await self._get(pager_id, tx)

    async def mark_failed(self, pager_id: UUID, *, resolution: str) -> None:
        await self.client.execute(
            pagers.update()
            .where(pagers.c.id == pager_id, pagers.c.status == PagerStatus.PUBLISHING.value)
            .values(
                status=PagerStatus.FAILED.value,
                resolution=resolution[:1000],
                closed_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )

    async def get_by_message(self, guild_id: int, channel_id: int, message_id: int) -> Pager | None:
        row = await self.client.fetch_one(
            sa.select(pagers.c.id).where(
                pagers.c.guild_id == guild_id,
                pagers.c.channel_id == channel_id,
                pagers.c.message_id == message_id,
            )
        )
        return None if row is None else await self.get(UUID(str(row["id"])))

    async def get(self, pager_id: UUID) -> Pager:
        async with self.client.connection() as connection:
            return await self._get(pager_id, connection)

    async def upsert_responder(self, pager_id: UUID, user_id: int, callsign: str) -> Pager:
        async with self.client.transaction() as tx:
            pager = await self._get_for_update(pager_id, tx)
            self._require_active(pager)
            if pager.creator_user_id == user_id:
                raise PagerCreatorCannotRespondError("Pager creator cannot register as a responder")
            await tx.execute(
                pg_insert(pager_responders)
                .values(
                    id=uuid.uuid4(),
                    pager_id=pager_id,
                    user_id=user_id,
                    callsign=callsign,
                )
                .on_conflict_do_update(
                    constraint="uq_pager_responder",
                    set_={"callsign": callsign, "updated_at": datetime.now(UTC)},
                )
            )
            await self._touch(pager_id, tx)
            return await self._get(pager_id, tx)

    async def remove_responder(self, pager_id: UUID, user_id: int) -> tuple[Pager, bool]:
        async with self.client.transaction() as tx:
            pager = await self._get_for_update(pager_id, tx)
            self._require_active(pager)
            result = await tx.execute(
                pager_responders.delete().where(
                    pager_responders.c.pager_id == pager_id,
                    pager_responders.c.user_id == user_id,
                )
            )
            removed = bool(result.rowcount)
            if removed:
                await self._touch(pager_id, tx)
            return await self._get(pager_id, tx), removed

    async def close(
        self,
        pager_id: UUID,
        *,
        status: PagerStatus,
        resolution: str,
        actor_user_id: int,
    ) -> tuple[Pager, bool]:
        if status not in {PagerStatus.CONCLUDED, PagerStatus.CANCELLED}:
            raise ValueError("Pager can only be concluded or cancelled")
        async with self.client.transaction() as tx:
            pager = await self._get_for_update(pager_id, tx)
            if pager.status.terminal:
                return pager, False
            self._require_active(pager)
            now = datetime.now(UTC)
            await tx.execute(
                pagers.update()
                .where(pagers.c.id == pager_id)
                .values(
                    status=status.value,
                    resolution=resolution,
                    closed_by_user_id=actor_user_id,
                    closed_at=now,
                    updated_at=now,
                )
            )
            return await self._get(pager_id, tx), True

    async def restore_active_after_delivery_failure(
        self,
        pager_id: UUID,
        *,
        expected_status: PagerStatus,
    ) -> Pager:
        """Undo a terminal transition when Discord could not publish its result."""

        if expected_status not in {PagerStatus.CONCLUDED, PagerStatus.CANCELLED}:
            raise ValueError("Expected status must be terminal")
        async with self.client.transaction() as tx:
            await self._get_for_update(pager_id, tx)
            await tx.execute(
                pagers.update()
                .where(pagers.c.id == pager_id, pagers.c.status == expected_status.value)
                .values(
                    status=PagerStatus.ACTIVE.value,
                    resolution=None,
                    closed_by_user_id=None,
                    closed_at=None,
                    updated_at=datetime.now(UTC),
                )
            )
            return await self._get(pager_id, tx)

    async def list_active(self, guild_id: int, *, offset: int, limit: int) -> tuple[tuple[Pager, ...], int]:
        where = (pagers.c.guild_id == guild_id, pagers.c.status == PagerStatus.ACTIVE.value)
        count_row = await self.client.fetch_one(sa.select(sa.func.count().label("count")).where(*where))
        total = int(count_row["count"] if count_row is not None else 0)
        rows = await self.client.fetch_all(
            sa.select(pagers.c.id)
            .where(*where)
            .order_by(pagers.c.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return tuple([await self.get(UUID(str(row["id"]))) for row in rows]), total

    async def list_for_user(
        self,
        guild_id: int,
        user_id: int,
        *,
        offset: int,
        limit: int,
    ) -> tuple[tuple[UserPager, ...], int]:
        membership_responder = pager_responders.alias("membership_responder")
        viewer_responder = pager_responders.alias("viewer_responder")
        relation = sa.or_(
            pagers.c.creator_user_id == user_id,
            sa.exists(
                sa.select(1)
                .select_from(membership_responder)
                .where(
                    membership_responder.c.pager_id == pagers.c.id,
                    membership_responder.c.user_id == user_id,
                )
                .correlate(pagers)
            ),
        )
        where = (pagers.c.guild_id == guild_id, pagers.c.status != PagerStatus.PUBLISHING.value, relation)
        count_row = await self.client.fetch_one(sa.select(sa.func.count().label("count")).where(*where))
        total = int(count_row["count"] if count_row is not None else 0)
        rows = await self.client.fetch_all(
            sa.select(pagers.c.id, pagers.c.creator_user_id, viewer_responder.c.callsign)
            .outerjoin(
                viewer_responder,
                sa.and_(
                    viewer_responder.c.pager_id == pagers.c.id,
                    viewer_responder.c.user_id == user_id,
                ),
            )
            .where(*where)
            .order_by(pagers.c.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        result: list[UserPager] = []
        for row in rows:
            result.append(
                UserPager(
                    pager=await self.get(UUID(str(row["id"]))),
                    creator=int(row["creator_user_id"]) == user_id,
                    responder_callsign=str(row["callsign"]) if row["callsign"] else None,
                )
            )
        return tuple(result), total

    async def _get_for_update(self, pager_id: UUID, tx: AsyncConnection) -> Pager:
        await tx.execute(sa.select(pagers.c.id).where(pagers.c.id == pager_id).with_for_update())
        return await self._get(pager_id, tx)

    async def _get(self, pager_id: UUID, connection: AsyncConnection) -> Pager:
        row = (
            (await connection.execute(sa.select(pagers).where(pagers.c.id == pager_id)))
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise LookupError(f"Pager {pager_id} was not found")
        responders = (
            (
                await connection.execute(
                    sa.select(pager_responders)
                    .where(pager_responders.c.pager_id == pager_id)
                    .order_by(pager_responders.c.created_at, pager_responders.c.user_id)
                )
            )
            .mappings()
            .all()
        )
        recipients = (
            (
                await connection.execute(
                    sa.select(pager_recipient_roles.c.role_id)
                    .where(pager_recipient_roles.c.pager_id == pager_id)
                    .order_by(pager_recipient_roles.c.position)
                )
            )
            .scalars()
            .all()
        )
        return Pager(
            id=UUID(str(row["id"])),
            guild_id=int(row["guild_id"]),
            channel_id=int(row["channel_id"]),
            message_id=int(row["message_id"]) if row["message_id"] is not None else None,
            number=int(row["number"]),
            creator_user_id=int(row["creator_user_id"]),
            creator_callsign=str(row["creator_callsign"]),
            recipient_role_ids=tuple(int(role_id) for role_id in recipients),
            location=str(row["location"]),
            situation=str(row["situation"]),
            status=PagerStatus(str(row["status"])),
            resolution=str(row["resolution"]) if row["resolution"] is not None else None,
            closed_by_user_id=(
                int(row["closed_by_user_id"]) if row["closed_by_user_id"] is not None else None
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            closed_at=row["closed_at"],
            responders=tuple(
                PagerResponder(
                    user_id=int(item["user_id"]),
                    callsign=str(item["callsign"]),
                    created_at=item["created_at"],
                    updated_at=item["updated_at"],
                )
                for item in responders
            ),
        )

    async def _touch(self, pager_id: UUID, tx: AsyncConnection) -> None:
        await tx.execute(pagers.update().where(pagers.c.id == pager_id).values(updated_at=datetime.now(UTC)))

    @staticmethod
    def _require_active(pager: Pager) -> None:
        if pager.status is not PagerStatus.ACTIVE:
            raise ValueError("Pager is not active")
