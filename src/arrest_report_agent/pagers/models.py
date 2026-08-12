"""Domain models for the deterministic pager subsystem."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PagerStatus(StrEnum):
    PUBLISHING = "publishing"
    ACTIVE = "active"
    CONCLUDED = "concluded"
    CANCELLED = "cancelled"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in {self.CONCLUDED, self.CANCELLED, self.FAILED}


class FrozenPagerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PagerResponder(FrozenPagerModel):
    user_id: int = Field(gt=0)
    callsign: str = Field(min_length=1, max_length=40)
    created_at: datetime
    updated_at: datetime


class Pager(FrozenPagerModel):
    id: UUID
    guild_id: int = Field(gt=0)
    channel_id: int = Field(gt=0)
    message_id: int | None = Field(default=None, gt=0)
    number: int = Field(ge=1, le=99_999)
    creator_user_id: int = Field(gt=0)
    creator_callsign: str = Field(min_length=1, max_length=40)
    recipient_role_id: int = Field(gt=0)
    location: str = Field(min_length=1, max_length=200)
    situation: str = Field(min_length=1, max_length=1000)
    status: PagerStatus
    resolution: str | None = Field(default=None, max_length=1000)
    closed_by_user_id: int | None = Field(default=None, gt=0)
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None
    responders: tuple[PagerResponder, ...] = ()

    @property
    def display_number(self) -> str:
        return f"{self.number:05d}"

    @property
    def jump_url(self) -> str | None:
        if self.message_id is None:
            return None
        return (
            f"https://discord.com/channels/{self.guild_id}/"
            f"{self.channel_id}/{self.message_id}"
        )


class UserPager(FrozenPagerModel):
    pager: Pager
    creator: bool
    responder_callsign: str | None = None

    @property
    def relationship(self) -> str:
        if self.creator and self.responder_callsign is not None:
            return "Emisor y respondiente"
        return "Emisor" if self.creator else "Respondiente"
