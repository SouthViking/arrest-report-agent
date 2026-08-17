"""Deterministic Mobile Data Computer subsystem."""

from .discord import MdcCog, MdcSearchModal, MdcVisibility, build_mdc_embed, build_mdc_info_embed
from .models import (
    MdcArrest,
    MdcCharacterAmbiguousError,
    MdcCharacterNotFoundError,
    MdcCharge,
    MdcFine,
    MdcLookupError,
    MdcProperty,
    MdcRecord,
    MdcSection,
    MdcVehicle,
    MdcWarrant,
    character_name_key,
    normalize_character_name,
)
from .service import MdcApiClient, MdcService

__all__ = [
    "MdcApiClient",
    "MdcArrest",
    "MdcCharacterAmbiguousError",
    "MdcCharacterNotFoundError",
    "MdcCog",
    "MdcCharge",
    "MdcFine",
    "MdcLookupError",
    "MdcProperty",
    "MdcRecord",
    "MdcSection",
    "MdcSearchModal",
    "MdcService",
    "MdcVisibility",
    "MdcVehicle",
    "MdcWarrant",
    "character_name_key",
    "normalize_character_name",
    "build_mdc_embed",
    "build_mdc_info_embed",
]
