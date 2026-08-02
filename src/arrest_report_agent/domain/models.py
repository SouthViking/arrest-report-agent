"""Validated data crossing application boundaries."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .enums import ContextState, FindingSeverity, InteractionMode, ReviewVerdict


class DiscordAttachment(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    message_id: int
    filename: str
    url: str
    content_type: str | None = None
    size: int = Field(ge=0)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    is_current: bool = False

    @property
    def is_image(self) -> bool:
        return bool(self.content_type and self.content_type.startswith("image/"))


class ConversationMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    author_id: int
    author_display: str
    author_is_bot: bool
    content: str
    created_at: datetime
    edited: bool = False
    reply_to_message_id: int | None = None
    reply_excerpt: str | None = None
    attachment_ids: list[int] = Field(default_factory=list)
    is_trigger: bool = False


class ReviewCase(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    guild_id: int
    parent_channel_id: int
    channel_id: int
    thread_id: int | None = None
    root_message_id: int
    trigger_message_id: int
    requester_user_id: int
    interaction_mode: InteractionMode
    case_subject: str | None = None
    messages: list[ConversationMessage]
    attachments: list[DiscordAttachment]
    context_state: ContextState = ContextState.COMPLETE
    context_notice: str | None = None


class ReviewFinding(BaseModel):
    severity: FindingSeverity
    title: str = Field(min_length=1, max_length=120)
    detail: str = Field(min_length=1, max_length=700)
    recommendation: str = Field(min_length=1, max_length=500)
    evidence: str | None = Field(default=None, max_length=300)


class ChargeAssessment(BaseModel):
    charge_id: int | None = None
    declared_charge: str = Field(min_length=1, max_length=160)
    alignment: Literal["supported", "unsupported", "uncertain", "not_found"]
    note: str = Field(min_length=1, max_length=500)


class ReviewPublication(BaseModel):
    """Terminal output accepted by the Discord publication tool."""

    verdict: ReviewVerdict
    summary: str = Field(min_length=1, max_length=500)
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=12)
    charges: list[ChargeAssessment] = Field(default_factory=list, max_length=10)
    next_step: str = Field(min_length=1, max_length=500)
    manual_review_reason: str | None = Field(default=None, max_length=500)

