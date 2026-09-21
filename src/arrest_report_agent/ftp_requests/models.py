"""Domain models for Field Training Program assistance requests."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class FtpRequestStatus(StrEnum):
    PUBLISHING = "publishing"
    PENDING = "pending"
    CLAIMED = "claimed"
    CANCELLED = "cancelled"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in {self.CLAIMED, self.CANCELLED, self.FAILED}


class FtpTrainingPhase(StrEnum):
    PHASE_1 = "phase_1"
    PHASE_2 = "phase_2"
    PHASE_3 = "phase_3"

    @property
    def label(self) -> str:
        return {
            self.PHASE_1: "Fase 1",
            self.PHASE_2: "Fase 2",
            self.PHASE_3: "Fase 3",
        }[self]


class FtpInstructorLevel(StrEnum):
    GENERAL = "general"
    SUPERVISOR = "supervisor"

    @property
    def label(self) -> str:
        return {
            self.GENERAL: "General",
            self.SUPERVISOR: "Supervisor",
        }[self]


class FtpTrainingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    guild_id: int = Field(gt=0)
    channel_id: int = Field(gt=0)
    message_id: int | None = Field(default=None, gt=0)
    requester_user_id: int = Field(gt=0)
    reason: str = Field(default="", max_length=500)
    training_phase: FtpTrainingPhase | None = None
    requested_instructor_level: FtpInstructorLevel | None = None
    scheduled_for: datetime | None = None
    status: FtpRequestStatus
    claimed_by_user_id: int | None = Field(default=None, gt=0)
    resolved_by_user_id: int | None = Field(default=None, gt=0)
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None

    @property
    def jump_url(self) -> str | None:
        if self.message_id is None:
            return None
        return f"https://discord.com/channels/{self.guild_id}/{self.channel_id}/{self.message_id}"

    @property
    def structured(self) -> bool:
        return (
            self.training_phase is not None
            and self.requested_instructor_level is not None
            and self.scheduled_for is not None
        )
