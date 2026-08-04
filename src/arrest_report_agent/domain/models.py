"""Validated data crossing application boundaries."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    input_kind: Literal["image", "file"] = "image"

    @property
    def is_image(self) -> bool:
        return self.input_kind == "image"


class ConversationMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    author_id: int
    author_display: str
    author_is_bot: bool
    author_is_assistant: bool = False
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
    requester_display_name: str | None = Field(default=None, max_length=100)
    requester_role_names: list[
        Annotated[str, Field(min_length=1, max_length=80)]
    ] = Field(default_factory=list, max_length=8)
    interaction_mode: InteractionMode
    response_route: Literal["inline_reply", "thread"] = "thread"
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

    @model_validator(mode="after")
    def validate_verdict_consistency(self) -> ReviewPublication:
        material_findings = [
            finding for finding in self.findings if finding.severity is FindingSeverity.REQUIRED
        ]
        unsupported_charges = [
            charge for charge in self.charges if charge.alignment == "unsupported"
        ]
        unverifiable_charges = [
            charge for charge in self.charges if charge.alignment == "not_found"
        ]
        if self.verdict is ReviewVerdict.APPROVED and (
            material_findings
            or unsupported_charges
            or unverifiable_charges
            or self.manual_review_reason
        ):
            raise ValueError("An approved review cannot contain unresolved material findings")
        if self.verdict is ReviewVerdict.CORRECTIONS_REQUIRED and not (
            material_findings or unsupported_charges
        ):
            raise ValueError("Corrections required must identify a material finding")
        return self
