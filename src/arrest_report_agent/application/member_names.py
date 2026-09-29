"""Shared deterministic matching for character and Discord member names."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from enum import IntEnum
from typing import Protocol

from .ports import GuildMemberProfile, GuildMemberSearchQuery, GuildMemberSearchResult


class MemberSearchDirectory(Protocol):
    async def search_members(
        self,
        guild_id: int,
        query: GuildMemberSearchQuery,
    ) -> GuildMemberSearchResult: ...


async def load_member_profiles(
    directory: MemberSearchDirectory,
    guild_id: int,
    *,
    role_ids: tuple[int, ...] = (),
) -> tuple[GuildMemberProfile, ...]:
    """Collect complete guild-local candidates so partial caches cannot imply uniqueness."""
    members: list[GuildMemberProfile] = []
    cursor: str | None = None
    for _ in range(50):
        result = await directory.search_members(
            guild_id,
            GuildMemberSearchQuery(
                query=None,
                user_ids=(),
                role_ids_any=role_ids,
                role_ids_all=(),
                role_ids_none=(),
                is_bot=False,
                joined_after=None,
                joined_before=None,
                sort_by="display_name",
                sort_direction="asc",
                limit=100,
                cursor=cursor,
            ),
        )
        if result.error is not None:
            raise RuntimeError(f"Discord member directory failed: {result.error}")
        members.extend(result.members)
        cursor = result.next_cursor
        if cursor is None:
            return tuple(members)
    raise RuntimeError("Discord member directory exceeded the pagination limit")


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


def unique_profile_match(
    character_name: str,
    candidates: Iterable[GuildMemberProfile],
) -> tuple[GuildMemberProfile | None, bool]:
    """Resolve only a unique strongest match; the second value signals ambiguity."""
    ranked = {
        candidate.user_id: (
            person_name_match_strength(
                character_name,
                person_name_aliases(candidate.display_name, candidate.username),
            ),
            candidate,
        )
        for candidate in candidates
        if not candidate.is_bot
    }
    strongest = max((strength for strength, _ in ranked.values()), default=0)
    if strongest <= 0:
        return None, False
    matches = [candidate for strength, candidate in ranked.values() if strength == strongest]
    if len(matches) != 1:
        return None, True
    return matches[0], False


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
