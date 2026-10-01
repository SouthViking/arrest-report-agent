"""Atomic reservations; Discord delivery is retried independently of booking state."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from arrest_report_agent.infrastructure.postgres import PostgresClient
from arrest_report_agent.infrastructure.postgres.schema import (
    ftp_availability_offers as offers,
)
from arrest_report_agent.infrastructure.postgres.schema import (
    ftp_availability_slots as slots,
)
from arrest_report_agent.infrastructure.postgres.schema import ftp_training_requests as requests

from .availability_models import AvailabilityOffer, AvailabilitySlot, block_times
from .models import FtpTrainingPhase


class AvailabilityRepository:
    def __init__(self, client: PostgresClient) -> None:
        self.client = client

    async def create(
        self,
        *,
        guild_id: int,
        channel_id: int,
        interaction_id: int,
        instructor_id: int,
        start: datetime,
        minutes: int,
        count: int,
        comments: str,
    ) -> AvailabilityOffer:
        times = block_times(start, minutes, count)
        if len(comments) > 500:
            raise ValueError("Los comentarios no pueden superar 500 caracteres.")
        now = datetime.now(UTC)
        if times[-1][1] <= now:
            raise ValueError("Todos esos bloques ya terminaron. Revisa la fecha, hora y zona horaria.")
        async with self.client.transaction() as tx:
            # Serializes simultaneous offers from this instructor, including unpublished drafts.
            await tx.execute(
                sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"ftp-offer:{guild_id}:{instructor_id}"},
            )
            existing: UUID | None = (
                await tx.execute(
                    sa.select(offers.c.id).where(
                        offers.c.interaction_id == interaction_id,
                        offers.c.guild_id == guild_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return await self._get(tx, guild_id, existing)
            overlap = (
                await tx.execute(
                    sa.select(slots.c.id)
                    .join(offers)
                    .where(
                        offers.c.guild_id == guild_id,
                        offers.c.instructor_id == instructor_id,
                        offers.c.status.in_(("publishing", "open", "closed")),
                        slots.c.status.in_(("free", "reserved")),
                        slots.c.ends_at > now,
                        slots.c.starts_at < times[-1][1],
                        slots.c.ends_at > times[0][0],
                    )
                    .limit(1)
                )
            ).first()
            if overlap is not None:
                raise ValueError("Ya tienes bloques disponibles o reservados que se cruzan con ese horario.")
            offer_id = uuid4()
            await tx.execute(
                offers.insert().values(
                    id=offer_id,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    interaction_id=interaction_id,
                    instructor_id=instructor_id,
                    comments=comments.strip(),
                    status="publishing",
                    created_at=now,
                    updated_at=now,
                    revision=0,
                )
            )
            await tx.execute(
                slots.insert(),
                [
                    {
                        "id": uuid4(),
                        "offer_id": offer_id,
                        "position": index,
                        "starts_at": begin,
                        "ends_at": end,
                        "status": "free",
                        "comments": "",
                        "notification_due": False,
                    }
                    for index, (begin, end) in enumerate(times, 1)
                ],
            )
            return await self._get(tx, guild_id, offer_id)

    async def publish(self, offer: AvailabilityOffer, message_id: int) -> AvailabilityOffer:
        async with self.client.transaction() as tx:
            await tx.execute(
                offers.update()
                .where(
                    offers.c.id == offer.id,
                    offers.c.guild_id == offer.guild_id,
                    offers.c.status == "publishing",
                )
                .values(message_id=message_id, status="open", sync_due_at=datetime.now(UTC))
            )
            return await self._get(tx, offer.guild_id, offer.id)

    async def fail_publication(self, offer: AvailabilityOffer) -> None:
        await self.client.execute(
            offers.update()
            .where(
                offers.c.id == offer.id,
                offers.c.status == "publishing",
            )
            .values(status="failed", updated_at=datetime.now(UTC))
        )

    async def get(self, guild_id: int, offer_id: UUID) -> AvailabilityOffer:
        async with self.client.connection() as tx:
            return await self._get(tx, guild_id, offer_id)

    async def by_message(self, guild_id: int, channel_id: int, message_id: int) -> AvailabilityOffer:
        async with self.client.connection() as tx:
            offer_id: UUID | None = (
                await tx.execute(
                    sa.select(offers.c.id).where(
                        offers.c.guild_id == guild_id,
                        offers.c.channel_id == channel_id,
                        offers.c.message_id == message_id,
                    )
                )
            ).scalar_one_or_none()
            if offer_id is None:
                raise LookupError("Esta disponibilidad ya no está disponible.")
            return await self._get(tx, guild_id, offer_id)

    async def reserve(
        self,
        guild_id: int,
        offer_id: UUID,
        slot_id: UUID,
        *,
        trainee_id: int,
        phase: FtpTrainingPhase,
        comments: str,
        now: datetime | None = None,
    ) -> AvailabilityOffer:
        current = now or datetime.now(UTC)
        if len(comments) > 500:
            raise ValueError("Los comentarios no pueden superar 500 caracteres.")
        async with self.client.transaction() as tx:
            offer = await self._get(tx, guild_id, offer_id, lock=True)
            slot = next((item for item in offer.slots if item.id == slot_id), None)
            if slot is None or offer.status != "open" or not slot.available(current):
                raise ValueError("Ese bloque ya fue reservado, cerrado o venció. Elige otro disponible.")
            request_id = uuid4()
            # Already assigned, NOT a pending request and NOT an evaluation marked as completed.
            # message_id stays null: several bookings share the same offer's message.
            await tx.execute(
                requests.insert().values(
                    id=request_id,
                    guild_id=guild_id,
                    channel_id=offer.channel_id,
                    requester_user_id=trainee_id,
                    reason=comments.strip(),
                    training_phase=phase.value,
                    requested_instructor_level="general",
                    scheduled_for=slot.starts_at,
                    status="claimed",
                    claimed_by_user_id=offer.instructor_id,
                    resolved_by_user_id=trainee_id,
                    created_at=current,
                    updated_at=current,
                    resolved_at=current,
                )
            )
            await tx.execute(
                slots.update()
                .where(slots.c.id == slot_id)
                .values(
                    status="reserved",
                    trainee_id=trainee_id,
                    phase=phase.value,
                    comments=comments.strip(),
                    request_id=request_id,
                    notification_due=True,
                )
            )
            await self._dirty(tx, offer_id, current)
            return await self._get(tx, guild_id, offer_id)

    async def close(self, guild_id: int, offer_id: UUID) -> AvailabilityOffer:
        async with self.client.transaction() as tx:
            offer = await self._get(tx, guild_id, offer_id, lock=True)
            if offer.status == "open":
                await tx.execute(
                    slots.update()
                    .where(
                        slots.c.offer_id == offer_id,
                        slots.c.status == "free",
                    )
                    .values(status="closed")
                )
                await tx.execute(offers.update().where(offers.c.id == offer_id).values(status="closed"))
                await self._dirty(tx, offer_id, datetime.now(UTC))
            return await self._get(tx, guild_id, offer_id)

    async def expire(self, guild_id: int, now: datetime) -> int:
        changed = 0
        async with self.client.transaction() as tx:
            # Recover drafts left behind by a process dying while sending to Discord.
            await tx.execute(
                offers.update()
                .where(
                    offers.c.guild_id == guild_id,
                    offers.c.status == "publishing",
                    offers.c.created_at < now - timedelta(minutes=15),
                )
                .values(status="failed", updated_at=now)
            )
            ids: Sequence[UUID] = (
                (
                    await tx.execute(
                        sa.select(offers.c.id)
                        .where(
                            offers.c.guild_id == guild_id,
                            offers.c.status == "open",
                            offers.c.id.in_(
                                sa.select(slots.c.offer_id).where(
                                    slots.c.status == "free",
                                    slots.c.ends_at <= now,
                                )
                            ),
                        )
                        .order_by(offers.c.id)
                        .limit(50)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for offer_id in ids:
                result = await tx.execute(
                    slots.update()
                    .where(
                        slots.c.offer_id == offer_id,
                        slots.c.status == "free",
                        slots.c.ends_at <= now,
                    )
                    .values(status="expired")
                )
                changed += result.rowcount
                await self._dirty(tx, offer_id, now)
        return changed

    async def due(self, guild_id: int, now: datetime) -> tuple[AvailabilityOffer, ...]:
        async with self.client.connection() as tx:
            ids: Sequence[UUID] = (
                (
                    await tx.execute(
                        sa.select(offers.c.id)
                        .where(
                            offers.c.guild_id == guild_id,
                            offers.c.sync_due_at <= now,
                        )
                        .order_by(offers.c.sync_due_at)
                        .limit(50)
                    )
                )
                .scalars()
                .all()
            )
            return tuple([await self._get(tx, guild_id, offer_id) for offer_id in ids])

    async def acknowledge(self, offer: AvailabilityOffer, retry_at: datetime | None = None) -> None:
        # A concurrent booking must retain its own pending edit.
        await self.client.execute(
            offers.update()
            .where(
                offers.c.id == offer.id,
                offers.c.revision == offer.revision,
            )
            .values(sync_due_at=retry_at)
        )

    async def notified(self, slot_id: UUID) -> None:
        await self.client.execute(slots.update().where(slots.c.id == slot_id).values(notification_due=False))

    @staticmethod
    async def _dirty(tx: AsyncConnection, offer_id: UUID, now: datetime) -> None:
        await tx.execute(
            offers.update()
            .where(offers.c.id == offer_id)
            .values(
                revision=offers.c.revision + 1,
                updated_at=now,
                sync_due_at=now,
            )
        )

    @staticmethod
    async def _get(
        tx: AsyncConnection, guild_id: int, offer_id: UUID, *, lock: bool = False
    ) -> AvailabilityOffer:
        query = sa.select(offers).where(offers.c.guild_id == guild_id, offers.c.id == offer_id)
        row = (await tx.execute(query.with_for_update() if lock else query)).mappings().one_or_none()
        if row is None:
            raise LookupError("Esta disponibilidad ya no está disponible.")
        rows = (
            await tx.execute(sa.select(slots).where(slots.c.offer_id == offer_id).order_by(slots.c.position))
        ).mappings()
        return AvailabilityOffer.model_validate(
            {
                **dict(row),
                "slots": tuple(AvailabilitySlot.model_validate(dict(slot)) for slot in rows),
            }
        )
