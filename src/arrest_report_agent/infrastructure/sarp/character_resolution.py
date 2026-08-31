"""Shared exact character-name resolution for SARP-backed features."""

from __future__ import annotations

import re
import unicodedata
from typing import Protocol

from .models import SarpCharacterSearchResult


class CharacterSearchClient(Protocol):
    async def search_characters(self, name: str) -> list[SarpCharacterSearchResult]: ...


class CharacterNotFoundError(LookupError):
    """No exact normalized character matched a SARP search."""


class CharacterAmbiguousError(LookupError):
    """More than one exact normalized character matched a SARP search."""


def normalize_character_name(value: str) -> str:
    """Normalize presentation differences without introducing fuzzy matching."""

    normalized = unicodedata.normalize("NFKC", value).replace("_", " ")
    return re.sub(r"\s+", " ", normalized).strip()


def character_name_key(value: str) -> str:
    """Return the case-insensitive exact-match key for one character name."""

    return normalize_character_name(value).casefold()


async def resolve_exact_character(
    client: CharacterSearchClient,
    raw_name: str,
) -> SarpCharacterSearchResult:
    """Resolve exactly one character while sharing normalization across consumers."""

    display_name = normalize_character_name(raw_name)
    if not display_name or len(display_name) > 100:
        raise CharacterNotFoundError(display_name)
    matches = await client.search_characters(display_name.replace(" ", "_"))
    expected_key = character_name_key(display_name)
    exact = [item for item in matches if character_name_key(item.name) == expected_key]
    if not exact:
        raise CharacterNotFoundError(display_name)
    if len(exact) != 1:
        raise CharacterAmbiguousError(display_name)
    return exact[0]
