"""Error classification and safe roleplay-facing messages."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import discord
import openai
from agents.exceptions import AgentsException, MaxTurnsExceeded, ModelBehaviorError

from arrest_report_agent.config import PersonaFailureMessages
from arrest_report_agent.domain import ErrorKind
from arrest_report_agent.domain.errors import ContextUnavailableError, PublicationError


@dataclass(frozen=True)
class ClassifiedError:
    kind: ErrorKind
    retryable: bool


def classify_error(error: BaseException) -> ClassifiedError:
    """Map provider, runner and Discord failures to stable operational classes."""

    if isinstance(error, (asyncio.TimeoutError, TimeoutError, openai.APITimeoutError)):
        return ClassifiedError(ErrorKind.TIMEOUT, True)
    if isinstance(error, openai.RateLimitError):
        detail = f"{getattr(error, 'code', '')} {getattr(error, 'body', '')}".casefold()
        if "quota" in detail or "insufficient_quota" in detail:
            return ClassifiedError(ErrorKind.QUOTA_EXCEEDED, False)
        return ClassifiedError(ErrorKind.RATE_LIMITED, True)
    if isinstance(error, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return ClassifiedError(ErrorKind.AUTH_CONFIGURATION, False)
    if isinstance(error, (openai.InternalServerError, openai.APIConnectionError)):
        return ClassifiedError(ErrorKind.PROVIDER_INTERNAL, True)
    if isinstance(error, openai.APIStatusError) and error.status_code >= 500:
        return ClassifiedError(ErrorKind.PROVIDER_INTERNAL, True)
    if isinstance(error, (MaxTurnsExceeded, ModelBehaviorError, AgentsException)):
        return ClassifiedError(ErrorKind.AGENT_EXECUTION, False)
    if isinstance(error, (discord.Forbidden, discord.HTTPException, PublicationError)):
        return ClassifiedError(ErrorKind.DISCORD_FAILURE, True)
    if isinstance(error, ContextUnavailableError):
        return ClassifiedError(ErrorKind.CONTEXT_UNAVAILABLE, True)
    return ClassifiedError(ErrorKind.UNKNOWN, False)


def public_error_message(kind: ErrorKind, messages: PersonaFailureMessages) -> str:
    """Return a contextual message without leaking provider or report data."""

    by_kind = {
        ErrorKind.QUOTA_EXCEEDED: messages.quota_exceeded,
        ErrorKind.RATE_LIMITED: messages.rate_limited,
        ErrorKind.TIMEOUT: messages.timeout,
        ErrorKind.PROVIDER_INTERNAL: messages.provider_internal,
        ErrorKind.AUTH_CONFIGURATION: messages.auth_configuration,
        ErrorKind.AGENT_EXECUTION: messages.agent_execution,
        ErrorKind.DISCORD_FAILURE: messages.discord_failure,
        ErrorKind.CONTEXT_UNAVAILABLE: messages.context_unavailable,
        ErrorKind.UNKNOWN: messages.unknown,
    }
    return by_kind[kind]
