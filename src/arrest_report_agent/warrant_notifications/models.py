"""Persisted Discord notification state for judicial orders."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class WarrantNotificationStatus(StrEnum):
    PUBLISHING = "publishing"
    PENDING = "pending"
    SIGNED = "signed"
    REJECTED = "rejected"
    FAILED = "failed"

    @property
    def resolved(self) -> bool:
        return self in {self.SIGNED, self.REJECTED}


class WarrantNotificationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    guild_id: int = Field(gt=0)
    interaction_id: int = Field(gt=0)
    channel_id: int = Field(gt=0)
    message_id: int | None = Field(default=None, gt=0)
    requester_user_id: int = Field(gt=0)
    report_url: str = Field(min_length=1, max_length=1000)
    warrant_id: int = Field(gt=0)
    warrant_type: str
    status: WarrantNotificationStatus
    resolved_by_user_id: int | None = Field(default=None, gt=0)
    rejection_reason: str | None = Field(default=None, max_length=500)
    result_message_id: int | None = Field(default=None, gt=0)
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None

    @property
    def jump_url(self) -> str | None:
        if self.message_id is None:
            return None
        return f"https://discord.com/channels/{self.guild_id}/{self.channel_id}/{self.message_id}"
