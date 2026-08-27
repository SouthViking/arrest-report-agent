"""Shared deterministic matching for character and Discord member names."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from enum import IntEnum


class PersonNameMatchStrength(IntEnum):
    """Ordered confidence levels used to avoid fuzzy or arbitrary matches."""

    NONE = 0
    INITIAL_AND_SURNAME = 1
    EXACT_TERMS = 2
    EXACT_ALIAS = 3


def normalize_person_name(value: str) -> str:
    """Build an accent-insensitive comparison key without changing display copy."""

    decomposed = unicodedata.normalize("NFKD", value.casefold().replace("_", " "))
    unaccented = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.findall(r"[^\W_]+", unaccented, flags=re.UNICODE))


def person_name_aliases(*sources: str) -> tuple[str, ...]:
    """Split multi-character Discord names into normalized independent aliases."""

    aliases: list[str] = []
    for source in sources:
        aliases.extend(normalize_person_name(value) for value in re.split(r"[/|]", source) if value.strip())
    return tuple(dict.fromkeys(alias for alias in aliases if alias))


def person_name_match_strength(character_name: str, aliases: Iterable[str]) -> int:
    """Return the strongest deterministic match between one character and aliases."""

    query = normalize_person_name(character_name)
    query_tokens = _tokens(query)
    if len(query_tokens) < 2:
        return PersonNameMatchStrength.NONE

    best = PersonNameMatchStrength.NONE
    for raw_alias in aliases:
        alias = normalize_person_name(raw_alias)
        if not alias:
            continue
        if alias == query:
            return PersonNameMatchStrength.EXACT_ALIAS

        alias_tokens = _tokens(alias)
        if all(token in alias_tokens for token in query_tokens):
            best = max(best, PersonNameMatchStrength.EXACT_TERMS)
            continue
        if _initial_and_surname_match(query_tokens, alias_tokens):
            best = max(best, PersonNameMatchStrength.INITIAL_AND_SURNAME)
    return int(best)


def _initial_and_surname_match(
    query_tokens: tuple[str, ...],
    alias_tokens: tuple[str, ...],
) -> bool:
    if len(alias_tokens) < 2 or len(alias_tokens[0]) != 1:
        return False
    if alias_tokens[0] != query_tokens[0][0]:
        return False
    query_surnames = query_tokens[1:]
    alias_surnames = alias_tokens[1:]
    return alias_surnames == query_surnames or alias_tokens[-1] == query_tokens[-1]


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(value.split())
