"""Validated model capabilities, runtime defaults and token pricing."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from importlib.resources import files
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
ImageDetail = Literal["low", "high", "auto", "original"]
TextVerbosity = Literal["low", "medium", "high"]
ServiceTier = Literal["default", "flex", "priority", "fast"]


class TokenRates(BaseModel):
    """USD rates for one catalog token unit."""

    model_config = ConfigDict(frozen=True)

    input: Decimal = Field(ge=0)
    cached_input: Decimal = Field(ge=0)
    cache_write: Decimal = Field(ge=0)
    output: Decimal = Field(ge=0)


class ContextPricingBand(BaseModel):
    """One pricing band selected from a request's input-token count."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    max_input_tokens: int | None = Field(default=None, gt=0)
    rates_per_unit: TokenRates


class ServiceTierPricing(BaseModel):
    """Pricing bands for one API service tier."""

    model_config = ConfigDict(frozen=True)

    display_name: str = Field(min_length=1)
    context_bands: tuple[ContextPricingBand, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_context_bands(self) -> ServiceTierPricing:
        bounded = [band.max_input_tokens for band in self.context_bands[:-1]]
        if any(limit is None for limit in bounded):
            raise ValueError("only the final context pricing band may be unbounded")
        if self.context_bands[-1].max_input_tokens is not None:
            raise ValueError("the final context pricing band must be unbounded")
        numeric_limits = [limit for limit in bounded if limit is not None]
        if numeric_limits != sorted(numeric_limits) or len(set(numeric_limits)) != len(numeric_limits):
            raise ValueError("context pricing bands must have unique ascending limits")
        return self

    def select_band(self, input_tokens: int) -> ContextPricingBand:
        for band in self.context_bands:
            if band.max_input_tokens is None or input_tokens <= band.max_input_tokens:
                return band
        raise RuntimeError("unreachable context pricing configuration")


class ModelRuntimeDefaults(BaseModel):
    """Model-dependent settings that should not be repeated in the environment."""

    model_config = ConfigDict(frozen=True)

    image_detail: ImageDetail
    text_verbosity: TextVerbosity
    max_output_tokens: int = Field(ge=256)
    service_tier: ServiceTier


class SupportedModel(BaseModel):
    """One supported model and its validated capabilities."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    aliases: tuple[str, ...] = ()
    supported_reasoning_efforts: tuple[ReasoningEffort, ...] = Field(min_length=1)
    supported_image_details: tuple[ImageDetail, ...] = Field(min_length=1)
    supported_text_verbosity: tuple[TextVerbosity, ...] = Field(min_length=1)
    runtime_defaults: ModelRuntimeDefaults
    pricing: dict[ServiceTier, ServiceTierPricing]

    @model_validator(mode="after")
    def validate_defaults(self) -> SupportedModel:
        if len(set(self.supported_reasoning_efforts)) != len(self.supported_reasoning_efforts):
            raise ValueError("supported reasoning efforts must be unique")
        if self.runtime_defaults.image_detail not in self.supported_image_details:
            raise ValueError("default image detail is not supported by the model")
        if self.runtime_defaults.text_verbosity not in self.supported_text_verbosity:
            raise ValueError("default text verbosity is not supported by the model")
        if self.runtime_defaults.service_tier not in self.pricing:
            raise ValueError("default service tier has no pricing definition")
        return self


class ModelCatalogPayload(BaseModel):
    """Top-level schema for the packaged model catalog."""

    model_config = ConfigDict(frozen=True)

    schema_version: str = Field(min_length=1)
    pricing_version: str = Field(min_length=1)
    pricing_source_url: str = Field(pattern=r"^https://")
    currency: Literal["USD"]
    token_price_unit: int = Field(gt=0)
    models: tuple[SupportedModel, ...] = Field(min_length=1)


@dataclass(frozen=True)
class ResolvedModel:
    """Runtime-safe model definition paired with a supported reasoning effort."""

    definition: SupportedModel
    reasoning_effort: ReasoningEffort
    catalog_schema_version: str
    pricing_version: str
    pricing_source_url: str
    currency: str
    token_price_unit: int

    @property
    def id(self) -> str:
        return self.definition.id

    @property
    def runtime_defaults(self) -> ModelRuntimeDefaults:
        return self.definition.runtime_defaults

    @property
    def service_tier_pricing(self) -> ServiceTierPricing:
        return self.definition.pricing[self.runtime_defaults.service_tier]


class ModelCatalog:
    """Lookup supported models and reject unknown runtime combinations at startup."""

    def __init__(self, payload: ModelCatalogPayload) -> None:
        self.payload = payload
        definitions: dict[str, SupportedModel] = {}
        for model in payload.models:
            identifiers = (model.id, *model.aliases)
            for identifier in identifiers:
                if identifier in definitions:
                    raise ValueError(f"duplicate model identifier in catalog: {identifier}")
                definitions[identifier] = model
        self._definitions = definitions

    @classmethod
    def load_default(cls) -> ModelCatalog:
        resource = files("arrest_report_agent.config").joinpath("data/models.json")
        payload = ModelCatalogPayload.model_validate_json(resource.read_text(encoding="utf-8"))
        return cls(payload)

    def resolve(self, model_id: str, reasoning_effort: str) -> ResolvedModel:
        normalized_model = model_id.strip()
        normalized_effort = reasoning_effort.strip().casefold()
        definition = self._definitions.get(normalized_model)
        if definition is None:
            supported = ", ".join(model.id for model in self.payload.models)
            raise ValueError(f"unsupported OPENAI_MODEL {normalized_model!r}; supported models: {supported}")
        if normalized_effort not in definition.supported_reasoning_efforts:
            supported_efforts = ", ".join(definition.supported_reasoning_efforts)
            raise ValueError(
                f"unsupported OPENAI_REASONING_EFFORT {normalized_effort!r} for "
                f"{definition.id}; supported efforts: {supported_efforts}"
            )
        return ResolvedModel(
            definition=definition,
            reasoning_effort=normalized_effort,
            catalog_schema_version=self.payload.schema_version,
            pricing_version=self.payload.pricing_version,
            pricing_source_url=self.payload.pricing_source_url,
            currency=self.payload.currency,
            token_price_unit=self.payload.token_price_unit,
        )
