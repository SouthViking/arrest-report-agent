"""Domain models for structured arrest-report submissions."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PublicationStatus(StrEnum):
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"


class InitialReviewStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ArrestReportAssetCategory(StrEnum):
    SUSPECT_PHOTO = "suspect_photo"
    SUPPORTING_IMAGE = "supporting_image"


class ArrestReportAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    submission_id: UUID
    category: ArrestReportAssetCategory
    position: int = Field(ge=0)
    object_key: str = Field(min_length=1, max_length=500)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=100)
    size_bytes: int = Field(gt=0)
    created_at: datetime


class PendingArrestReportAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    category: ArrestReportAssetCategory
    position: int = Field(ge=0)
    object_key: str = Field(min_length=1, max_length=500)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=100)
    size_bytes: int = Field(gt=0)


class ArrestReportSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    guild_id: int = Field(gt=0)
    interaction_id: int = Field(gt=0)
    owner_user_id: int = Field(gt=0)
    parent_channel_id: int = Field(gt=0)
    root_message_id: int | None = Field(default=None, gt=0)
    thread_id: int | None = Field(default=None, gt=0)
    sarp_arrest_id: int | None = Field(default=None, gt=0)
    suspect_name: str = Field(min_length=1, max_length=100)
    publication_status: PublicationStatus
    initial_review_status: InitialReviewStatus
    created_at: datetime
    updated_at: datetime
    assets: tuple[ArrestReportAsset, ...] = ()

    @property
    def jump_url(self) -> str | None:
        if self.root_message_id is None:
            return None
        return f"https://discord.com/channels/{self.guild_id}/{self.parent_channel_id}/{self.root_message_id}"
