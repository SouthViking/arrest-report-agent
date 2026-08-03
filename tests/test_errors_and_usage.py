from __future__ import annotations

import logging
from decimal import Decimal

import httpx
import openai
from agents.usage import Usage

from arrest_report_agent.application.errors import classify_error, public_error_message
from arrest_report_agent.application.usage import summarize_usage
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.domain import ErrorKind


def response(status_code: int) -> httpx.Response:
    request = httpx.Request("POST", "https://api.openai.test/v1/responses")
    return httpx.Response(status_code, request=request)


def test_error_classifier_distinguishes_quota_and_rate_limit(settings) -> None:
    quota = openai.RateLimitError(
        "quota",
        response=response(429),
        body={"error": {"code": "insufficient_quota"}},
    )
    limited = openai.RateLimitError("slow down", response=response(429), body={})

    assert classify_error(quota).kind is ErrorKind.QUOTA_EXCEEDED
    assert classify_error(limited).kind is ErrorKind.RATE_LIMITED
    message = public_error_message(ErrorKind.QUOTA_EXCEEDED)
    assert "consumo de IA" in message
    assert settings.identity.display_name not in message
    assert "informa" not in message


def test_error_classifier_handles_timeout_auth_and_provider() -> None:
    request = httpx.Request("POST", "https://api.openai.test/v1/responses")

    assert classify_error(openai.APITimeoutError(request=request)).kind is ErrorKind.TIMEOUT
    assert (
        classify_error(
            openai.AuthenticationError("bad key", response=response(401), body={})
        ).kind
        is ErrorKind.AUTH_CONFIGURATION
    )
    assert (
        classify_error(
            openai.InternalServerError("upstream", response=response(500), body={})
        ).kind
        is ErrorKind.PROVIDER_INTERNAL
    )


def test_usage_summary_estimates_cost_without_content(settings) -> None:
    usage = Usage(
        requests=2,
        input_tokens=10_000,
        input_tokens_details={"cached_tokens": 2_000, "cache_write_tokens": 0},
        output_tokens=1_000,
        output_tokens_details={"reasoning_tokens": 600},
        total_tokens=11_000,
    )

    summary = summarize_usage(usage, settings)

    assert summary.cached_input_tokens == 2_000
    assert summary.reasoning_tokens == 600
    assert summary.estimated_cost_usd == Decimal("0.035500")


def test_error_log_keeps_structured_fields_and_full_traceback(caplog) -> None:
    logger = logging.getLogger("tests.error_logging")

    try:
        raise RuntimeError("provider exploded")
    except RuntimeError as error:
        with caplog.at_level(logging.ERROR, logger=logger.name):
            log_event(
                logger,
                "error",
                "review_failed",
                exc_info=error,
                technical_reference="42-1-unknown",
                **exception_fields(error),
            )

    assert "event=review_failed" in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    assert "technical_reference=42-1-unknown" in caplog.text
    assert "Traceback (most recent call last)" in caplog.text
    assert "RuntimeError: provider exploded" in caplog.text
