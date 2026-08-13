"""Deterministic suggestion inbox subsystem."""

from .discord import SuggestionCog
from .models import (
    Suggestion,
    SuggestionCategory,
    SuggestionCooldownError,
    SuggestionEvent,
    SuggestionEventType,
    SuggestionLimitReachedError,
    SuggestionNotFoundError,
    SuggestionStatus,
)
from .repository import (
    MAX_PENDING_SUGGESTIONS,
    SUBMISSION_COOLDOWN_SECONDS,
    SuggestionRepository,
)

__all__ = [
    "MAX_PENDING_SUGGESTIONS",
    "SUBMISSION_COOLDOWN_SECONDS",
    "Suggestion",
    "SuggestionCategory",
    "SuggestionCog",
    "SuggestionCooldownError",
    "SuggestionEvent",
    "SuggestionEventType",
    "SuggestionLimitReachedError",
    "SuggestionNotFoundError",
    "SuggestionRepository",
    "SuggestionStatus",
]
