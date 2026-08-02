"""Token usage summaries for non-sensitive operational logs."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from agents.usage import Usage

from arrest_report_agent.config import AppSettings


@dataclass(frozen=True)
class UsageSummary:
    requests: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    estimated_cost_usd: Decimal


def summarize_usage(usage: Usage, settings: AppSettings) -> UsageSummary:
    cached = int(getattr(usage.input_tokens_details, "cached_tokens", 0) or 0)
    reasoning = int(getattr(usage.output_tokens_details, "reasoning_tokens", 0) or 0)
    uncached = max(usage.input_tokens - cached, 0)
    million = Decimal(1_000_000)
    estimated = (
        Decimal(uncached) * settings.openai_input_cost_per_million
        + Decimal(cached) * settings.openai_cached_input_cost_per_million
        + Decimal(usage.output_tokens) * settings.openai_output_cost_per_million
    ) / million
    return UsageSummary(
        requests=usage.requests,
        input_tokens=usage.input_tokens,
        cached_input_tokens=cached,
        output_tokens=usage.output_tokens,
        reasoning_tokens=reasoning,
        estimated_cost_usd=estimated.quantize(Decimal("0.000001")),
    )

