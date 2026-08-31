"""Project SARP exceptions into stable, agent-safe failure codes."""

from __future__ import annotations

from .errors import SarpError, SarpNotFoundError, SarpRateLimitError


def classify_sarp_failure(error: Exception) -> tuple[str, float | None]:
    if isinstance(error, SarpNotFoundError):
        return "not_found", None
    if isinstance(error, SarpRateLimitError):
        return "rate_limited", error.rate_limit.retry_after_seconds
    if isinstance(error, ValueError):
        return "invalid_request", None
    if isinstance(error, SarpError):
        return "temporarily_unavailable", None
    return "temporarily_unavailable", None
