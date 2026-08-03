"""Raw token usage and catalog-backed cost summaries for operational logs."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from typing import TypedDict

from agents.usage import RequestUsage, Usage

from arrest_report_agent.config import ResolvedModel

COST_PRECISION = Decimal("0.00000001")


class UsageLogFields(TypedDict):
    requests: int
    input_tokens: int
    cached_input_tokens: int
    cache_write_input_tokens: int
    uncached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    visible_output_tokens: int
    total_tokens: int
    uncached_input_cost_usd: float
    cached_input_cost_usd: float
    cache_write_input_cost_usd: float
    output_cost_usd: float
    estimated_cost_usd: float
    pricing_context_bands: str
    pricing_service_tier: str
    pricing_tier_name: str
    pricing_version: str
    pricing_currency: str
    pricing_token_unit: int
    pricing_source_url: str
    model_catalog_schema_version: str
    pricing_calculation_basis: str
    priced_requests: int


@dataclass(frozen=True)
class UsageSummary:
    """Raw provider counters plus derived token and USD cost breakdowns."""

    requests: int
    input_tokens: int
    cached_input_tokens: int
    cache_write_input_tokens: int
    uncached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    visible_output_tokens: int
    total_tokens: int
    uncached_input_cost_usd: Decimal
    cached_input_cost_usd: Decimal
    cache_write_input_cost_usd: Decimal
    output_cost_usd: Decimal
    estimated_cost_usd: Decimal
    pricing_context_bands: str
    pricing_service_tier: str
    pricing_tier_name: str
    pricing_version: str
    pricing_currency: str
    pricing_token_unit: int
    pricing_source_url: str
    model_catalog_schema_version: str
    pricing_calculation_basis: str
    priced_requests: int

    def to_log_fields(self) -> UsageLogFields:
        """Return JSON-native counters and monetary values without sensitive content."""

        return {
            "requests": self.requests,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "cache_write_input_tokens": self.cache_write_input_tokens,
            "uncached_input_tokens": self.uncached_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "visible_output_tokens": self.visible_output_tokens,
            "total_tokens": self.total_tokens,
            "uncached_input_cost_usd": float(self.uncached_input_cost_usd),
            "cached_input_cost_usd": float(self.cached_input_cost_usd),
            "cache_write_input_cost_usd": float(self.cache_write_input_cost_usd),
            "output_cost_usd": float(self.output_cost_usd),
            "estimated_cost_usd": float(self.estimated_cost_usd),
            "pricing_context_bands": self.pricing_context_bands,
            "pricing_service_tier": self.pricing_service_tier,
            "pricing_tier_name": self.pricing_tier_name,
            "pricing_version": self.pricing_version,
            "pricing_currency": self.pricing_currency,
            "pricing_token_unit": self.pricing_token_unit,
            "pricing_source_url": self.pricing_source_url,
            "model_catalog_schema_version": self.model_catalog_schema_version,
            "pricing_calculation_basis": self.pricing_calculation_basis,
            "priced_requests": self.priced_requests,
        }


def summarize_usage(usage: Usage, model: ResolvedModel) -> UsageSummary:
    """Calculate costs per request so long-context pricing is never inferred from totals."""

    cached = int(getattr(usage.input_tokens_details, "cached_tokens", 0) or 0)
    cache_write = int(getattr(usage.input_tokens_details, "cache_write_tokens", 0) or 0)
    reasoning = int(getattr(usage.output_tokens_details, "reasoning_tokens", 0) or 0)
    uncached = max(usage.input_tokens - cached - cache_write, 0)
    visible_output = max(usage.output_tokens - reasoning, 0)

    uncached_cost = Decimal(0)
    cached_cost = Decimal(0)
    cache_write_cost = Decimal(0)
    output_cost = Decimal(0)
    band_counts: Counter[str] = Counter()
    tier = model.service_tier_pricing
    unit = Decimal(model.token_price_unit)

    entries, calculation_basis = _request_entries(usage)
    for entry in entries:
        entry_cached = int(getattr(entry.input_tokens_details, "cached_tokens", 0) or 0)
        entry_cache_write = int(
            getattr(entry.input_tokens_details, "cache_write_tokens", 0) or 0
        )
        entry_uncached = max(entry.input_tokens - entry_cached - entry_cache_write, 0)
        band = tier.select_band(entry.input_tokens)
        rates = band.rates_per_unit
        band_counts[band.id] += 1
        uncached_cost += Decimal(entry_uncached) * rates.input / unit
        cached_cost += Decimal(entry_cached) * rates.cached_input / unit
        cache_write_cost += Decimal(entry_cache_write) * rates.cache_write / unit
        output_cost += Decimal(entry.output_tokens) * rates.output / unit

    estimated = uncached_cost + cached_cost + cache_write_cost + output_cost
    return UsageSummary(
        requests=usage.requests,
        input_tokens=usage.input_tokens,
        cached_input_tokens=cached,
        cache_write_input_tokens=cache_write,
        uncached_input_tokens=uncached,
        output_tokens=usage.output_tokens,
        reasoning_tokens=reasoning,
        visible_output_tokens=visible_output,
        total_tokens=usage.total_tokens,
        uncached_input_cost_usd=_money(uncached_cost),
        cached_input_cost_usd=_money(cached_cost),
        cache_write_input_cost_usd=_money(cache_write_cost),
        output_cost_usd=_money(output_cost),
        estimated_cost_usd=_money(estimated),
        pricing_context_bands=",".join(
            f"{band_id}:{count}" for band_id, count in sorted(band_counts.items())
        )
        or "none",
        pricing_service_tier=model.runtime_defaults.service_tier,
        pricing_tier_name=tier.display_name,
        pricing_version=model.pricing_version,
        pricing_currency=model.currency,
        pricing_token_unit=model.token_price_unit,
        pricing_source_url=model.pricing_source_url,
        model_catalog_schema_version=model.catalog_schema_version,
        pricing_calculation_basis=calculation_basis,
        priced_requests=len(entries),
    )


def _request_entries(usage: Usage) -> tuple[list[RequestUsage], str]:
    if usage.request_usage_entries:
        return usage.request_usage_entries, "per_request"
    if usage.input_tokens or usage.output_tokens or usage.total_tokens:
        return (
            [
                RequestUsage(
                    input_tokens=usage.input_tokens,
                    input_tokens_details=usage.input_tokens_details,
                    output_tokens=usage.output_tokens,
                    output_tokens_details=usage.output_tokens_details,
                    total_tokens=usage.total_tokens,
                )
            ],
            "aggregate_fallback",
        )
    return [], "no_usage"


def _money(value: Decimal) -> Decimal:
    return value.quantize(COST_PRECISION)
