"""Validated data crossing application boundaries."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .enums import (
    ContextState,
    FindingSeverity,
    InteractionMode,
    ReviewScope,
    ReviewVerdict,
)

DiscordRoleName = Annotated[str, Field(min_length=1, max_length=80)]


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
    author_role_names: list[DiscordRoleName] = Field(default_factory=list, max_length=8)
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
    requester_role_names: list[DiscordRoleName] = Field(default_factory=list, max_length=8)
    interaction_mode: InteractionMode
    response_route: Literal["agent_choice", "inline_reply", "thread"] = "thread"
    case_subject: str | None = None
    messages: list[ConversationMessage]
    attachments: list[DiscordAttachment]
    context_state: ContextState = ContextState.COMPLETE
    context_notice: str | None = None


class ReviewFinding(BaseModel):
    severity: FindingSeverity
    title: str = Field(
        min_length=1,
        max_length=120,
        description="Neutral, professional Spanish title without jokes, insults or slang.",
    )
    detail: str = Field(
        min_length=1,
        max_length=700,
        description="Clear technical explanation in correct, neutral Spanish.",
    )
    recommendation: str = Field(
        min_length=1,
        max_length=500,
        description=(
            "Actionable correction in correct, neutral Spanish, addressing the author with "
            "second-person singular tú imperatives such as 'incorpora' or 'corrige'; never use "
            "formal usted forms such as 'incorpore' or 'corrija', nor an infinitive instruction."
        ),
    )
    evidence: str | None = Field(
        default=None,
        max_length=300,
        description="Brief neutral reference to the relevant material.",
    )


class ChargeAssessment(BaseModel):
    charge_id: int | None = None
    declared_charge: str = Field(min_length=1, max_length=160)
    alignment: Literal["supported", "unsupported", "uncertain", "not_found"]
    note: str = Field(
        min_length=1,
        max_length=500,
        description="Technical assessment in correct, neutral Spanish without persona slang.",
    )


class ReviewPublication(BaseModel):
    """Terminal output accepted by the Discord publication tool."""

    review_scope: ReviewScope = Field(
        default=ReviewScope.FULL,
        description=(
            "Exact scope requested by the user: full report, narrative only, charges only, or a "
            "targeted point. Approved applies only within this scope."
        ),
    )
    focus: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description=(
            "Specific user-requested point when review_scope is targeted; otherwise set null."
        ),
    )
    verdict: ReviewVerdict = Field(
        description="Verdict limited to review_scope; partial approval is not full-report approval."
    )
    summary: str = Field(
        min_length=1,
        max_length=500,
        description="Clear opening verdict using the persona's review-summary voice.",
    )
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=12)
    charges: list[ChargeAssessment] = Field(default_factory=list, max_length=10)
    manual_review_reason: str | None = Field(
        default=None,
        max_length=500,
        description="Formal and neutral reason for human escalation when required.",
    )

    @model_validator(mode="after")
    def validate_verdict_consistency(self) -> ReviewPublication:
        if self.review_scope is ReviewScope.TARGETED and self.focus is None:
            raise ValueError("A targeted review must identify its focus")
        if self.review_scope is not ReviewScope.TARGETED and self.focus is not None:
            raise ValueError("Only a targeted review can define focus")
        if self.review_scope is ReviewScope.NARRATIVE and self.charges:
            raise ValueError("A narrative-only review cannot publish charge assessments")
        material_findings = [
            finding for finding in self.findings if finding.severity is FindingSeverity.REQUIRED
        ]
        if self.verdict is ReviewVerdict.APPROVED and (
            material_findings
            or self.manual_review_reason
        ):
            raise ValueError("An approved review cannot contain unresolved material findings")
        if self.verdict is ReviewVerdict.CORRECTIONS_REQUIRED and not material_findings:
            raise ValueError("Corrections required must identify a material finding")
        return self
