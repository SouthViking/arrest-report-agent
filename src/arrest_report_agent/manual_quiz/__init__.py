"""Shared manual-learning quiz subsystem for catalogue and agent-authored questions."""

from arrest_report_agent.domain import (
    QuizAnswerMode,
    QuizCategory,
    QuizDifficulty,
    QuizDurationSeconds,
)

from .discord import (
    ManualQuizCog,
    ManualQuizConfigurationModal,
    ManualQuizDiscordPublisher,
    ManualQuizView,
)
from .models import (
    MVP_ANSWER_MODES,
    ActiveQuizSessionError,
    NoQuizQuestionsError,
    QuizCooldownError,
    QuizOption,
    QuizQuestion,
    QuizQuestionReservation,
    QuizQuestionStatus,
    build_dynamic_question,
)
from .repository import ManualQuizRepository
from .sessions import ManualQuizSessionRegistry

__all__ = [
    "MVP_ANSWER_MODES",
    "ActiveQuizSessionError",
    "ManualQuizCog",
    "ManualQuizConfigurationModal",
    "ManualQuizDiscordPublisher",
    "ManualQuizRepository",
    "ManualQuizSessionRegistry",
    "ManualQuizView",
    "NoQuizQuestionsError",
    "QuizAnswerMode",
    "QuizCategory",
    "QuizCooldownError",
    "QuizDifficulty",
    "QuizDurationSeconds",
    "QuizOption",
    "QuizQuestion",
    "QuizQuestionReservation",
    "QuizQuestionStatus",
    "build_dynamic_question",
]
