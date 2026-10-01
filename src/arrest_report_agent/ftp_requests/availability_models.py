"""Instructor availability is independent of a trainee's pending request."""

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from .models import FtpTrainingPhase

BLOCK_MINUTES = (30, 45, 60, 90)
MAX_BLOCKS = 5


def block_times(start: datetime, minutes: int, count: int) -> tuple[tuple[datetime, datetime], ...]:
    if start.tzinfo is None or minutes not in BLOCK_MINUTES or not 1 <= count <= MAX_BLOCKS:
        raise ValueError("Selecciona una duración y cantidad de bloques válidas.")
    start = start.astimezone(UTC)
    duration = timedelta(minutes=minutes)
    return tuple((start + index * duration, start + (index + 1) * duration) for index in range(count))


class AvailabilitySlot(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    offer_id: UUID
    position: int
    starts_at: datetime
    ends_at: datetime
    status: Literal["free", "reserved", "closed", "expired"]
    trainee_id: int | None = None
    phase: FtpTrainingPhase | None = None
    comments: str = ""
    request_id: UUID | None = None
    notification_due: bool = False

    def available(self, now: datetime) -> bool:
        return self.status == "free" and self.ends_at > now


class AvailabilityOffer(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    guild_id: int
    channel_id: int
    message_id: int | None = None
    interaction_id: int
    instructor_id: int
    comments: str
    status: Literal["publishing", "open", "closed", "failed"]
    created_at: datetime
    updated_at: datetime
    revision: int
    sync_due_at: datetime | None = None
    slots: tuple[AvailabilitySlot, ...] = ()

    @property
    def jump_url(self) -> str | None:
        if self.message_id is None:
            return None
        return f"https://discord.com/channels/{self.guild_id}/{self.channel_id}/{self.message_id}"

    def available_slots(self, now: datetime) -> tuple[AvailabilitySlot, ...]:
        if self.status != "open":
            return ()
        return tuple(slot for slot in self.slots if slot.available(now))
