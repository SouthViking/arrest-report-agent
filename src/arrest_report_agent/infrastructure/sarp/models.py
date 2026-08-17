"""Typed transport models shared by every SARP API endpoint."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PayloadT = TypeVar("PayloadT")


class SarpEnvelope(BaseModel):
    """Documented JSON envelope returned by the SARP API."""

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    message: str = ""
    http_code: int = Field(alias="httpCode", ge=100, le=599)
    source: str | None = None
    payload: object | None = None


@dataclass(frozen=True, slots=True)
class SarpRateLimit:
    """Rate-limit metadata exposed through the documented response headers."""

    limit: str | None = None
    policy: str | None = None
    reset: str | None = None
    retry_after_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class SarpResponse[PayloadT]:
    """Validated API response plus transport metadata useful to callers."""

    message: str
    http_code: int
    source: str | None
    payload: PayloadT
    rate_limit: SarpRateLimit
    request_id: str | None = None


class SarpPenalCodeCategory(BaseModel):
    """Category returned by the documented police penal-code endpoint."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: int = Field(gt=0)
    title: str = Field(min_length=1)
    description: str | None = None
    min_code: str | int | None = None
    max_code: str | int | None = None


class SarpPenalCodeCharge(BaseModel):
    """Charge returned by the documented police penal-code endpoint."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: int = Field(gt=0)
    category: int = Field(ge=0)
    category_title: str | None = Field(default=None, min_length=1)
    code: str = Field(min_length=1)
    charge: str = Field(min_length=1)
    description: str = ""
    type: str = Field(min_length=1)
    time: int | float | None = None
    hours: int | float | None = None
    parole: int | float | None = None
    fine: int | float | None = None
    points: int | float | None = None

    @model_validator(mode="after")
    def validate_category_title(self) -> SarpPenalCodeCharge:
        if self.category != 0 and self.category_title is None:
            raise ValueError("SARP categorized charges must include category_title")
        return self


class SarpPenalCodePayload(BaseModel):
    """Complete structured penal-code snapshot returned by SARP."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    categories: list[SarpPenalCodeCategory] = Field(min_length=1)
    charges: list[SarpPenalCodeCharge] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_catalog_integrity(self) -> SarpPenalCodePayload:
        category_ids = [category.id for category in self.categories]
        category_id_set = set(category_ids)
        if len(category_id_set) != len(category_ids):
            raise ValueError("SARP penal-code category IDs must be unique")
        charge_ids = [charge.id for charge in self.charges]
        if len(set(charge_ids)) != len(charge_ids):
            raise ValueError("SARP penal-code charge IDs must be unique")
        unknown_categories = {
            charge.category
            for charge in self.charges
            if charge.category != 0 and charge.category not in category_id_set
        }
        if unknown_categories:
            raise ValueError(
                f"SARP penal-code charges reference unknown categories: {sorted(unknown_categories)}"
            )
        return self


class SarpCharacterSearchResult(BaseModel):
    """Compact character identity returned by the prefix-search endpoint."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=100)


class SarpCharacterPreview(BaseModel):
    """Public character data; consumers must explicitly project IC-safe fields."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=100)
    jailed: int | bool | None = None


class SarpCharacterVehicle(BaseModel):
    """Vehicle included in a character's public possessions."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: int = Field(gt=0)
    model: int | str | None = None
    model_name: str | None = None
    name: str | None = None
    plate: str | None = None
    ownership_type: str | None = None


class SarpCharacterProperty(BaseModel):
    """Property included in a character's public possessions."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: int = Field(gt=0)
    name: str | None = None
    address: str | None = None
    type: int | str | None = None
    subtype: int | str | None = None


class SarpCharacterPossessions(BaseModel):
    """Public vehicles and properties owned by one character."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    properties: list[SarpCharacterProperty] = Field(default_factory=list)
    vehicles: list[SarpCharacterVehicle] = Field(default_factory=list)


class SarpPoliceCharge(BaseModel):
    """Compact charge embedded in police arrest and warrant records."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    charge_id: int = Field(gt=0)
    code: str | None = None
    charge: str = Field(min_length=1)
    type: str | None = None
    hours: int | float | None = None
    fine: int | float | None = None


class SarpFine(BaseModel):
    """Fine issued to a character."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: int = Field(gt=0)
    character_id: int = Field(gt=0)
    fine: str = Field(min_length=1)
    cost: int | float = Field(ge=0)
    is_paid: int | bool
    paid_at: datetime | None = None
    created_at: datetime | None = None


class SarpFinePage(BaseModel):
    """Paginated fine list."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    items: list[SarpFine] = Field(default_factory=list)
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    limit: int = Field(ge=1, le=100)
    pages: int = Field(ge=0)


class SarpWarrant(BaseModel):
    """Non-confidential warrant returned by the police API."""

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    id: int = Field(gt=0)
    suspect_id: int = Field(gt=0)
    description: str = ""
    types: tuple[str, ...] = Field(default_factory=tuple, alias="type")
    risk: str | None = None
    status: int
    closed_at: datetime | None = None
    created_at: datetime | None = None
    charges: list[SarpPoliceCharge] = Field(default_factory=list)

    @field_validator("types", mode="before")
    @classmethod
    def normalize_types(cls, value: object) -> object:
        if value is None:
            return ()
        if isinstance(value, str):
            return (value,)
        return value

    @property
    def active(self) -> bool:
        return self.status == 1 and self.closed_at is None


class SarpWarrantPage(BaseModel):
    """Paginated non-confidential warrant list."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    items: list[SarpWarrant] = Field(default_factory=list)
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    limit: int = Field(ge=1, le=100)
    pages: int = Field(ge=0)


class SarpArrest(BaseModel):
    """Police arrest record for one suspect."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: int = Field(gt=0)
    suspect_id: int = Field(gt=0)
    description: str = ""
    created_at: datetime | None = None
    charges: list[SarpPoliceCharge] = Field(default_factory=list)


class SarpArrestPage(BaseModel):
    """Paginated police arrest list."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    items: list[SarpArrest] = Field(default_factory=list)
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    limit: int = Field(ge=1, le=100)
    pages: int = Field(ge=0)
