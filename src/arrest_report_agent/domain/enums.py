"""Stable domain enumerations."""

from __future__ import annotations

from enum import IntEnum, StrEnum


class ReviewVerdict(StrEnum):
    APPROVED = "approved"
    CORRECTIONS_REQUIRED = "corrections_required"
    INCONCLUSIVE = "inconclusive"


class ReviewScope(StrEnum):
    FULL = "full"
    NARRATIVE = "narrative"
    CHARGES = "charges"
    TARGETED = "targeted"


class FindingSeverity(StrEnum):
    REQUIRED = "required"
    WARNING = "warning"
    OBSERVATION = "observation"


class ReviewIssueType(StrEnum):
    """Stable taxonomy used by review storage and deterministic analytics."""

    DEPUTY_IDENTITY = "deputy_identity"
    CALLSIGN_MISSING = "callsign_missing"
    CALLSIGN_FORMAT = "callsign_format"
    ORTHOGRAPHY = "orthography"
    FACTS_INSUFFICIENT = "facts_insufficient"
    CHRONOLOGY_OR_LOCATION = "chronology_or_location"
    DESCRIPTION_MISMATCH = "description_mismatch"
    EVIDENCE_REFERENCE = "evidence_reference"
    CHARGE_NOT_JUSTIFIED = "charge_not_justified"
    CHARGE_MISMATCH = "charge_mismatch"
    REQUIRED_MATERIAL_MISSING = "required_material_missing"
    REPORT_FORMAT = "report_format"
    OTHER = "other"


class ContextState(StrEnum):
    COMPLETE = "complete"
    TRUNCATED = "truncated"
    UNAVAILABLE = "unavailable"


class InteractionMode(StrEnum):
    INITIAL_REVIEW = "initial_review"
    THREAD_FOLLOW_UP = "thread_follow_up"


class RequestIntent(StrEnum):
    REVIEW = "review"
    CONVERSATION = "conversation"
    OUT_OF_SCOPE = "out_of_scope"


class AgentAccessLevel(StrEnum):
    """Capabilities granted to one agent execution."""

    FULL = "full"
    REPORT_OWNER = "report_owner"


class ConversationResponseRoute(StrEnum):
    """Agent-selectable route without exposing arbitrary Discord destinations."""

    REPLY = "reply"
    THREAD = "thread"


class AgentActivity(StrEnum):
    """User-facing activity of one in-flight agent interaction."""

    CONVERSATION = "conversation"
    KNOWLEDGE = "knowledge"
    MANUAL_INDEX = "manual_index"
    MANUAL_SEARCH = "manual_search"
    MANUAL_READ = "manual_read"
    FREQUENCIES = "frequencies"
    PERSONNEL = "personnel"
    RECORDS = "records"
    EVIDENCE = "evidence"
    EVIDENCE_BAG = "evidence_bag"
    REVIEW = "review"
    RESPONSE = "response"


class QuizAnswerMode(StrEnum):
    """Answer interactions supported by the shared manual-quiz renderer."""

    SINGLE_CHOICE = "single_choice"
    TRUE_FALSE = "true_false"
    MULTIPLE_SELECT = "multiple_select"
    SHORT_ANSWER = "short_answer"
    ORDERING = "ordering"
    MATCHING = "matching"


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


class QuizDurationSeconds(IntEnum):
    """Bounded public-answer windows shared by commands and agent tools."""

    TEN = 10
    FIFTEEN = 15
    THIRTY = 30
    ONE_MINUTE = 60
    THREE_MINUTES = 180
    FIVE_MINUTES = 300


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
