"""Validated data crossing application boundaries."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .enums import (
    ContextState,
    FindingSeverity,
    InteractionMode,
    QuizAnswerMode,
    QuizCategory,
    QuizDifficulty,
    QuizDurationSeconds,
    ReviewIssueType,
    ReviewScope,
    ReviewVerdict,
)

DiscordRoleName = Annotated[str, Field(min_length=1, max_length=80)]


class ManualQuizPublication(BaseModel):
    """Validated one-off question requested by the general-purpose agent."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    prompt: str = Field(
        min_length=1,
        max_length=1500,
        description="Question shown publicly, grounded in the faction manual.",
    )
    options: tuple[Annotated[str, Field(min_length=1, max_length=300)], ...] = Field(
        min_length=2,
        max_length=10,
        description="Ordered answer options without numeric prefixes.",
    )
    correct_option_number: int = Field(
        ge=1,
        le=10,
        description="One-based position of the only correct option.",
    )
    explanation: str = Field(
        min_length=1,
        max_length=1500,
        description="Learning-oriented explanation revealed only after the question closes.",
    )
    source_reference: str = Field(
        default="Manual faccionario",
        min_length=1,
        max_length=300,
        description="Manual section or source supporting the correct answer.",
    )
    answer_mode: Literal[
        QuizAnswerMode.SINGLE_CHOICE,
        QuizAnswerMode.TRUE_FALSE,
    ] = QuizAnswerMode.SINGLE_CHOICE
    category: QuizCategory = QuizCategory.GENERAL
    difficulty: QuizDifficulty = QuizDifficulty.INTERMEDIATE
    duration_seconds: QuizDurationSeconds = QuizDurationSeconds.THIRTY

    @model_validator(mode="after")
    def validate_question_contract(self) -> ManualQuizPublication:
        normalized_options = tuple(option.casefold() for option in self.options)
        if len(normalized_options) != len(set(normalized_options)):
            raise ValueError("dynamic question options must be unique")
        if self.correct_option_number > len(self.options):
            raise ValueError("correct_option_number must reference an available option")
        if self.answer_mode is QuizAnswerMode.TRUE_FALSE and normalized_options != (
            "verdadero",
            "falso",
        ):
            raise ValueError("true_false questions must use Verdadero and Falso in that order")
        return self


class ManualQuizDelivery(BaseModel):
    """Structured outcome returned to the agent after attempting publication."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    status: Literal["published", "active_question", "cooldown", "unavailable"]
    question_id: str | None = None
    retry_after_seconds: int | None = Field(default=None, ge=1)


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
    ambient_attachment_summary: str | None = Field(default=None, max_length=500)
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
    structured_submission_id: UUID | None = None
    structured_report_id: int | None = Field(default=None, gt=0)
    structured_suspect_name: str | None = Field(default=None, min_length=1, max_length=100)
    messages: list[ConversationMessage]
    attachments: list[DiscordAttachment]
    context_state: ContextState = ContextState.COMPLETE
    context_notice: str | None = None


class ReviewFinding(BaseModel):
    issue_type: ReviewIssueType = Field(
        description=(
            "Stable category for deterministic report analytics. Choose the closest specific "
            "category; use other only when none applies. Severity remains independent from this "
            "classification."
        )
    )
    severity: FindingSeverity = Field(
        description=(
            "Impact-based classification: required only for a mandatory or material correctable "
            "deficiency; warning for a relevant non-blocking improvement; observation for an "
            "optional clarity, style or future-report learning suggestion."
        )
    )
    title: str = Field(
        min_length=1,
        max_length=120,
        description=(
            "Neutral, professional Spanish title beginning with an uppercase letter and without "
            "jokes, insults or slang."
        ),
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
            "formal usted forms such as 'incorpore' or 'corrija', nor an infinitive instruction. "
            "For optional future-report advice, explicitly address future cases without requiring "
            "changes to the current report."
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
    alignment: Literal["supported", "unsupported", "uncertain", "not_found"] = Field(
        description=(
            "Semantic alignment with the documented conduct and full sequence; do not require "
            "literal formulas or isolated phrases when the elements follow reasonably from context."
        )
    )
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
        description=("Specific user-requested point when review_scope is targeted; otherwise set null."),
    )
    verdict: ReviewVerdict = Field(
        description=(
            "Verdict limited to review_scope; only required findings block approval, and partial "
            "approval is not full-report approval."
        )
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
        if self.verdict is ReviewVerdict.APPROVED and (material_findings or self.manual_review_reason):
            raise ValueError("An approved review cannot contain unresolved material findings")
        if self.verdict is ReviewVerdict.CORRECTIONS_REQUIRED and not material_findings:
            raise ValueError("Corrections required must identify a material finding")
        return self
