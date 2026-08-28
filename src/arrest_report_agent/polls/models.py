"""Durable metadata models for native Discord polls."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class PollRecord(BaseModel):
    """Minimal index for a poll whose live content remains in Discord."""

    model_config = ConfigDict(frozen=True)

    message_id: int
    guild_id: int
    channel_id: int
    creator_user_id: int
    created_at: datetime
    expires_at: datetime
    ended_at: datetime | None = None
    deleted_at: datetime | None = None
    last_synced_at: datetime | None = None
    last_activity_at: datetime | None = None

