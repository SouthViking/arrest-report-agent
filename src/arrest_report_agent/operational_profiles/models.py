"""Domain models for reusable per-guild member metadata."""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

CALLSIGN_PATTERN = re.compile(r"^[0-9]{2,3}[A-Z]$")


class CallsignFormatError(ValueError):
    """Raised when an operational callsign does not use the supported format."""


def normalize_callsign(value: str) -> str:
    """Normalize and validate callsigns shared by commands and pager responses."""

    normalized = value.strip().upper()
    if not CALLSIGN_PATTERN.fullmatch(normalized):
        raise CallsignFormatError("Callsign must contain 2-3 digits followed by one letter from A-Z")
    return normalized


class MemberProfile(BaseModel):
    """The small operational profile associated with one Discord member."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    guild_id: int = Field(gt=0)
    user_id: int = Field(gt=0)
    callsign: str = Field(min_length=1, max_length=40)
    created_at: datetime
    updated_at: datetime
