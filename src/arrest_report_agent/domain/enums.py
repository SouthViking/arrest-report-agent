"""Stable domain enumerations."""

from __future__ import annotations

from enum import StrEnum


class ReviewVerdict(StrEnum):
    APPROVED = "approved"
    CORRECTIONS_REQUIRED = "corrections_required"
    INCONCLUSIVE = "inconclusive"


class FindingSeverity(StrEnum):
    REQUIRED = "required"
    WARNING = "warning"
    OBSERVATION = "observation"


class ContextState(StrEnum):
    COMPLETE = "complete"
    TRUNCATED = "truncated"
    UNAVAILABLE = "unavailable"


class InteractionMode(StrEnum):
    INITIAL_REVIEW = "initial_review"
    THREAD_FOLLOW_UP = "thread_follow_up"


class ErrorKind(StrEnum):
    QUOTA_EXCEEDED = "quota_exceeded"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    PROVIDER_INTERNAL = "provider_internal"
    AUTH_CONFIGURATION = "auth_configuration"
    AGENT_EXECUTION = "agent_execution"
    DISCORD_FAILURE = "discord_failure"
    CONTEXT_UNAVAILABLE = "context_unavailable"
    UNKNOWN = "unknown"

