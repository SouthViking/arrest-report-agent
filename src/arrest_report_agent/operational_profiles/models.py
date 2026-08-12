"""Domain models for reusable per-guild member metadata."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class MemberProfile(BaseModel):
    """The small operational profile associated with one Discord member."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    guild_id: int = Field(gt=0)
    user_id: int = Field(gt=0)
    callsign: str = Field(min_length=1, max_length=40)
    created_at: datetime
    updated_at: datetime
