"""Domain models for the deterministic suggestion inbox."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SuggestionCategory(StrEnum):
    AUTOMATION = "automation"
    PROCEDURE = "procedure"
    DOCUMENTATION = "documentation"
    EXPERIENCE = "experience"
    CORRECTION = "correction"
    OTHER = "other"


class SuggestionStatus(StrEnum):
    PENDING = "pending"
    ARCHIVED = "archived"


class SuggestionEventType(StrEnum):
    SUBMITTED = "submitted"
    STATUS_CHANGED = "status_changed"


class Suggestion(BaseModel):
    """One guild-scoped suggestion persisted independently from Discord messages."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    guild_id: int = Field(gt=0)
    number: int = Field(gt=0)
    author_user_id: int = Field(gt=0)
    source_channel_id: int = Field(gt=0)
    category: SuggestionCategory
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=1500)
    status: SuggestionStatus
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None

    @property
    def reference(self) -> str:
        return f"SUG-{self.number:05d}"


class SuggestionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    suggestion_id: UUID
    event_type: SuggestionEventType
    actor_user_id: int = Field(gt=0)
    from_status: SuggestionStatus | None
    to_status: SuggestionStatus
    comment: str | None = Field(default=None, max_length=1000)
    created_at: datetime


class SuggestionLimitReachedError(ValueError):
    """Raised when a member already has the maximum pending suggestions."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(f"Pending suggestion limit reached ({limit})")


class SuggestionCooldownError(ValueError):
    """Raised when a member submits suggestions too quickly."""

    def __init__(self, remaining_seconds: int) -> None:
        self.remaining_seconds = remaining_seconds
        super().__init__(f"Suggestion cooldown active ({remaining_seconds}s remaining)")


class SuggestionNotFoundError(LookupError):
    """Raised when a guild does not contain the requested suggestion number."""
