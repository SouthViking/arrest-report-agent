"""Validated contracts for the deterministic manual quiz catalogue."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class QuizAnswerMode(StrEnum):
    """Evaluation strategies supported by the persisted catalogue."""

    SINGLE_CHOICE = "single_choice"
    TRUE_FALSE = "true_false"
    MULTIPLE_SELECT = "multiple_select"
    SHORT_ANSWER = "short_answer"
    ORDERING = "ordering"
    MATCHING = "matching"


MVP_ANSWER_MODES = (QuizAnswerMode.SINGLE_CHOICE, QuizAnswerMode.TRUE_FALSE)


class QuizCategory(StrEnum):
    GENERAL = "general"
    RADIO_CODE = "radio_code"
    PROCEDURE = "procedure"
    SCENARIO = "scenario"
    IDENTIFY_ERROR = "identify_error"
    LEGAL = "legal"
    REPORTS = "reports"


class QuizDifficulty(StrEnum):
    BASIC = "basic"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"


class QuizQuestionStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class QuizOption(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: UUID
    position: int = Field(ge=0, le=24)
    text: str = Field(min_length=1, max_length=300)
    is_correct: bool


class QuizQuestion(BaseModel):
    """One immutable question snapshot used throughout a Discord interaction."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: UUID
    guild_id: int = Field(gt=0)
    slug: str = Field(pattern=r"^[a-z0-9]+(?:_[a-z0-9]+)*$", min_length=1, max_length=120)
    answer_mode: QuizAnswerMode
    category: QuizCategory
    difficulty: QuizDifficulty
    prompt: str = Field(min_length=1, max_length=1500)
    explanation: str = Field(min_length=1, max_length=1500)
    source_reference: str = Field(min_length=1, max_length=300)
    status: QuizQuestionStatus
    revision: int = Field(ge=1)
    created_by: int | None = Field(default=None, gt=0)
    options: tuple[QuizOption, ...]
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_answer_contract(self) -> QuizQuestion:
        positions = tuple(option.position for option in self.options)
        if positions != tuple(range(len(self.options))):
            raise ValueError("quiz option positions must be contiguous and ordered")
        if len({option.id for option in self.options}) != len(self.options):
            raise ValueError("quiz options cannot contain duplicate IDs")
        correct_count = sum(option.is_correct for option in self.options)
        if self.answer_mode is QuizAnswerMode.SINGLE_CHOICE:
            if not 2 <= len(self.options) <= 10 or correct_count != 1:
                raise ValueError("single-choice questions require 2-10 options and one answer")
        elif self.answer_mode is QuizAnswerMode.TRUE_FALSE:
            if len(self.options) != 2 or correct_count != 1:
                raise ValueError("true/false questions require two options and one answer")
        elif self.answer_mode is QuizAnswerMode.MULTIPLE_SELECT and (
            not 2 <= len(self.options) <= 10 or not 1 <= correct_count < len(self.options)
        ):
            raise ValueError("multiple-select questions require a non-empty proper answer set")
        return self

    @property
    def correct_options(self) -> tuple[QuizOption, ...]:
        return tuple(option for option in self.options if option.is_correct)

    def option(self, option_id: UUID) -> QuizOption | None:
        return next((option for option in self.options if option.id == option_id), None)


class QuizSessionError(RuntimeError):
    """Base class for expected in-memory session outcomes."""


class ActiveQuizSessionError(QuizSessionError):
    """Raised when a member already owns an unanswered question."""


class QuizCooldownError(QuizSessionError):
    def __init__(self, remaining_seconds: int) -> None:
        self.remaining_seconds = remaining_seconds
        super().__init__(f"Quiz cooldown active ({remaining_seconds}s remaining)")


class NoQuizQuestionsError(QuizSessionError):
    """Raised when the guild has no valid active MVP questions."""
