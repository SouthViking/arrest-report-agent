from __future__ import annotations

from decimal import Decimal

import pytest
from agents.usage import Usage

from arrest_report_agent.application.usage import summarize_usage
from arrest_report_agent.config import ModelCatalog


def test_catalog_resolves_supported_runtime_defaults_and_pricing() -> None:
    model = ModelCatalog.load_default().resolve("gpt-5.6-terra", "low")

    assert model.id == "gpt-5.6-terra"
    assert model.reasoning_effort == "low"
    assert model.runtime_defaults.image_detail == "original"
    assert model.runtime_defaults.text_verbosity == "low"
    assert model.runtime_defaults.max_output_tokens == 2500
    assert model.runtime_defaults.service_tier == "default"
    assert model.service_tier_pricing.select_band(272_000).id == "short"
    assert model.service_tier_pricing.select_band(272_001).id == "long"


@pytest.mark.parametrize(
    ("model_id", "effort", "expected"),
    [
        ("unknown-model", "low", "unsupported OPENAI_MODEL"),
        ("gpt-5.6-terra", "minimal", "unsupported OPENAI_REASONING_EFFORT"),
    ],
)
def test_catalog_rejects_unsupported_runtime_combinations(
    model_id: str,
    effort: str,
    expected: str,
) -> None:
    with pytest.raises(ValueError, match=expected):
        ModelCatalog.load_default().resolve(model_id, effort)


def test_cost_uses_each_request_context_band_instead_of_aggregate_total() -> None:
    model = ModelCatalog.load_default().resolve("gpt-5.6-terra", "low")
    usage = Usage()
    usage.add(
        Usage(
            requests=1,
            input_tokens=200_000,
            input_tokens_details={"cached_tokens": 0, "cache_write_tokens": 0},
            output_tokens=0,
            total_tokens=200_000,
        )
    )
    usage.add(
        Usage(
            requests=1,
            input_tokens=100_000,
            input_tokens_details={"cached_tokens": 0, "cache_write_tokens": 0},
            output_tokens=0,
            total_tokens=100_000,
        )
    )

    summary = summarize_usage(usage, model)

    assert summary.input_tokens == 300_000
    assert summary.pricing_context_bands == "short:2"
    assert summary.pricing_calculation_basis == "per_request"
    assert summary.priced_requests == 2
    assert summary.estimated_cost_usd == Decimal("0.60000000")
