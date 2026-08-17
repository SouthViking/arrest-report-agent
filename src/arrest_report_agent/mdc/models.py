"""IC-safe domain models for deterministic MDC lookups."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class MdcLookupError(RuntimeError):
    """Base failure for a lookup that cannot produce an MDC record."""


class MdcCharacterNotFoundError(MdcLookupError):
    """No exact normalized character name matched the search results."""


class MdcCharacterAmbiguousError(MdcLookupError):
    """More than one character unexpectedly matched the exact normalized name."""


class MdcSection(StrEnum):
    STATUS = "status"
    POSSESSIONS = "possessions"
    FINES = "fines"
    WARRANTS = "warrants"
    ARRESTS = "arrests"


class FrozenMdcModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MdcVehicle(FrozenMdcModel):
    name: str = Field(min_length=1, max_length=100)
    plate: str | None = Field(default=None, max_length=30)


class MdcProperty(FrozenMdcModel):
    name: str | None = Field(default=None, max_length=150)
    address: str | None = Field(default=None, max_length=200)


class MdcFine(FrozenMdcModel):
    description: str = Field(min_length=1, max_length=200)
    cost: int | float = Field(ge=0)
    created_at: datetime | None = None


class MdcCharge(FrozenMdcModel):
    code: str | None = Field(default=None, max_length=30)
    name: str = Field(min_length=1, max_length=200)


class MdcWarrant(FrozenMdcModel):
    description: str = Field(default="", max_length=1000)
    types: tuple[str, ...] = ()
    risk: str | None = Field(default=None, max_length=30)
    charges: tuple[MdcCharge, ...] = ()
    created_at: datetime | None = None


class MdcArrest(FrozenMdcModel):
    charges: tuple[MdcCharge, ...] = ()
    created_at: datetime | None = None


class MdcRecord(FrozenMdcModel):
    """Only fields an officer could reasonably consume IC through an MDC."""

    name: str = Field(min_length=1, max_length=100)
    incarcerated: bool | None = None
    vehicles: tuple[MdcVehicle, ...] = ()
    properties: tuple[MdcProperty, ...] = ()
    pending_fines: tuple[MdcFine, ...] = ()
    pending_fine_count: int | None = Field(default=None, ge=0)
    pending_fine_amount: int | float | None = Field(default=None, ge=0)
    active_warrants: tuple[MdcWarrant, ...] = ()
    active_warrant_count: int | None = Field(default=None, ge=0)
    arrests: tuple[MdcArrest, ...] = ()
    arrest_count: int | None = Field(default=None, ge=0)
    unavailable_sections: tuple[MdcSection, ...] = ()


def normalize_character_name(value: str) -> str:
    """Normalize user input and API names without applying fuzzy matching."""

    normalized = unicodedata.normalize("NFKC", value).replace("_", " ")
    return re.sub(r"\s+", " ", normalized).strip()


def character_name_key(value: str) -> str:
    """Return the case-insensitive exact-match key for one character name."""

    return normalize_character_name(value).casefold()
