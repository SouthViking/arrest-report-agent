"""Validated, compact search index over the official LSSD radio-code reference."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from difflib import SequenceMatcher
from importlib.resources import files
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RadioCodeCategory = Literal[
    "response_code",
    "ten_code",
    "situation_code",
    "phonetic",
]
RadioSearchField = Literal["code", "meaning", "aliases"]
RadioMatchMode = Literal["auto", "exact", "contains"]
RadioTermMatch = Literal["all", "any"]

DEFAULT_RADIO_SEARCH_FIELDS: tuple[RadioSearchField, ...] = (
    "code",
    "meaning",
    "aliases",
)
RADIO_FIELD_WEIGHTS: dict[RadioSearchField, int] = {
    "code": 100,
    "meaning": 70,
    "aliases": 50,
}
FUZZY_MIN_TERM_LENGTH = 4
FUZZY_MATCH_THRESHOLD = 0.82


@dataclass
class _MatchMetadata:
    score: int
    matched_queries: list[str] = dataclass_field(default_factory=list)
    matched_fields: set[RadioSearchField] = dataclass_field(default_factory=set)
    match_kinds: set[str] = dataclass_field(default_factory=set)


class RadioCodeEntry(BaseModel):
    """One canonical radio-code or phonetic-alphabet mapping."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1, max_length=32)
    category: RadioCodeCategory
    meaning: str = Field(min_length=1, max_length=500)
    aliases: tuple[str, ...] = Field(default_factory=tuple)


class RadioCodePayload(BaseModel):
    """Versioned packaged source extracted from the official faction manual."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(min_length=1, max_length=32)
    source: str = Field(min_length=1, max_length=256)
    entries: tuple[RadioCodeEntry, ...]


class RadioCodeCatalog:
    """Search a small official vocabulary without injecting it into every prompt."""

    def __init__(self, payload: RadioCodePayload, source_hash: str) -> None:
        if not payload.entries:
            raise ValueError("Radio-code catalog must contain at least one entry")
        unique_keys = {
            (entry.category, _normalize_code(entry.code)) for entry in payload.entries
        }
        if len(unique_keys) != len(payload.entries):
            raise ValueError("Radio-code entries must be unique within each category")

        self.version = payload.version
        self.source = payload.source
        self.entries = payload.entries
        self.source_hash = source_hash
        self._documents = tuple(_search_document(entry) for entry in self.entries)

    @classmethod
    def load_default(cls) -> RadioCodeCatalog:
        resource = files("arrest_report_agent.knowledge").joinpath("data/radio_codes.json")
        return cls.from_path(Path(str(resource)))

    @classmethod
    def from_path(cls, path: Path) -> RadioCodeCatalog:
        raw = path.read_bytes()
        payload = RadioCodePayload.model_validate(json.loads(raw))
        return cls(payload, hashlib.sha256(raw).hexdigest())

    @property
    def entry_count(self) -> int:
        return len(self.entries)

    def search(
        self,
        queries: list[str],
        categories: list[RadioCodeCategory] | None = None,
        fields: list[RadioSearchField] | None = None,
        match_mode: RadioMatchMode = "auto",
        term_match: RadioTermMatch = "all",
        fuzzy: bool = False,
        limit: int = 20,
    ) -> dict[str, object]:
        """Return a compact union of matches for one to ten independent queries."""

        normalized_queries = _deduplicate_queries(queries)
        if not normalized_queries:
            raise ValueError("At least one non-empty radio-code query is required")
        if len(normalized_queries) > 10:
            raise ValueError("At most ten radio-code queries can be requested at once")

        selected_categories = tuple(dict.fromkeys(categories or ()))
        selected_fields = tuple(dict.fromkeys(fields or DEFAULT_RADIO_SEARCH_FIELDS))
        if not selected_fields:
            raise ValueError("At least one radio-code search field is required")
        unknown_fields = set(selected_fields) - set(RADIO_FIELD_WEIGHTS)
        if unknown_fields:
            raise ValueError(f"Unknown radio-code search fields: {sorted(unknown_fields)}")
        if match_mode not in {"auto", "exact", "contains"}:
            raise ValueError("match_mode must be 'auto', 'exact' or 'contains'")
        if term_match not in {"all", "any"}:
            raise ValueError("term_match must be 'all' or 'any'")

        category_filter = set(selected_categories)
        matches: dict[int, _MatchMetadata] = {}
        unmatched_queries: list[str] = []
        for original_query, normalized_query in normalized_queries:
            query_matches = self._matches_for_query(
                normalized_query,
                category_filter=category_filter,
                fields=selected_fields,
                match_mode=match_mode,
                term_match=term_match,
                fuzzy=fuzzy,
            )
            if not query_matches:
                unmatched_queries.append(original_query)
                continue
            for entry_index, score, matched_fields, match_kind in query_matches:
                current = matches.setdefault(
                    entry_index,
                    _MatchMetadata(score=score),
                )
                current.score = max(current.score, score)
                current.matched_queries.append(original_query)
                current.matched_fields.update(matched_fields)
                current.match_kinds.add(match_kind)

        ordered = sorted(
            matches.items(),
            key=lambda item: (
                -item[1].score,
                _category_order(self.entries[item[0]].category),
                _natural_code_key(self.entries[item[0]].code),
            ),
        )
        bounded_limit = max(1, min(limit, 50))
        results: list[dict[str, object]] = []
        for entry_index, metadata in ordered[:bounded_limit]:
            entry = self.entries[entry_index]
            result: dict[str, object] = {
                "code": entry.code,
                "category": entry.category,
                "meaning": entry.meaning,
                "matched_queries": metadata.matched_queries,
                "matched_fields": sorted(
                    metadata.matched_fields,
                    key=tuple(RADIO_FIELD_WEIGHTS).index,
                ),
                "match_kinds": sorted(metadata.match_kinds),
            }
            if entry.aliases:
                result["aliases"] = list(entry.aliases)
            results.append(result)

        return {
            "version": self.version,
            "source": self.source,
            "results": results,
            "matched": len(ordered),
            "returned": len(results),
            "unmatched_queries": unmatched_queries,
        }

    def _matches_for_query(
        self,
        query: str,
        *,
        category_filter: set[RadioCodeCategory],
        fields: tuple[RadioSearchField, ...],
        match_mode: RadioMatchMode,
        term_match: RadioTermMatch,
        fuzzy: bool,
    ) -> list[tuple[int, int, set[RadioSearchField], str]]:
        candidates: list[tuple[int, int, set[RadioSearchField], str]] = []
        exact_candidates: list[tuple[int, int, set[RadioSearchField], str]] = []
        for index, entry in enumerate(self.entries):
            if category_filter and entry.category not in category_filter:
                continue
            document = self._documents[index]
            exact_fields = _exact_fields(query, document, fields)
            if exact_fields:
                exact_candidates.append(
                    (index, _score(exact_fields, 30), exact_fields, "exact")
                )
                continue
            if match_mode == "exact":
                continue
            contains_fields = _term_fields(query, document, fields, term_match=term_match)
            if contains_fields:
                candidates.append(
                    (index, _score(contains_fields, 10), contains_fields, "contains")
                )
                continue
            if fuzzy:
                fuzzy_fields = _fuzzy_fields(query, document, fields, term_match=term_match)
                if fuzzy_fields:
                    candidates.append(
                        (index, _score(fuzzy_fields, 5), fuzzy_fields, "fuzzy")
                    )

        if match_mode == "auto" and exact_candidates:
            return exact_candidates
        return [*exact_candidates, *candidates]


def _search_document(entry: RadioCodeEntry) -> dict[RadioSearchField, tuple[str, ...]]:
    aliases = tuple(_normalize_text(alias) for alias in entry.aliases)
    derived_aliases: tuple[str, ...] = ()
    if entry.category == "response_code":
        derived_aliases = (_normalize_text(f"código {entry.code}"),)
    return {
        "code": (_normalize_code(entry.code),),
        "meaning": (_normalize_text(entry.meaning),),
        "aliases": (*aliases, *derived_aliases),
    }


def _deduplicate_queries(queries: list[str]) -> list[tuple[str, str]]:
    selected: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_query in queries:
        original = raw_query.strip()
        normalized = _normalize_text(original)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        selected.append((original, normalized))
    return selected


def _exact_fields(
    query: str,
    document: dict[RadioSearchField, tuple[str, ...]],
    fields: tuple[RadioSearchField, ...],
) -> set[RadioSearchField]:
    code_query = _normalize_code(query)
    matched: set[RadioSearchField] = set()
    for field in fields:
        values = document[field]
        if field == "code":
            if code_query and code_query in values:
                matched.add(field)
        elif query in values:
            matched.add(field)
    return matched


def _term_fields(
    query: str,
    document: dict[RadioSearchField, tuple[str, ...]],
    fields: tuple[RadioSearchField, ...],
    *,
    term_match: RadioTermMatch,
) -> set[RadioSearchField]:
    terms = query.split()
    if not terms:
        return set()
    matched_by_term: list[set[RadioSearchField]] = []
    for term in terms:
        matched_by_term.append(
            {
                field
                for field in fields
                if any(term in value for value in document[field])
            }
        )
    if term_match == "all" and not all(matched_by_term):
        return set()
    if term_match == "any" and not any(matched_by_term):
        return set()
    return set().union(*matched_by_term)


def _fuzzy_fields(
    query: str,
    document: dict[RadioSearchField, tuple[str, ...]],
    fields: tuple[RadioSearchField, ...],
    *,
    term_match: RadioTermMatch,
) -> set[RadioSearchField]:
    terms = [term for term in query.split() if len(term) >= FUZZY_MIN_TERM_LENGTH]
    if not terms:
        return set()
    matched_by_term: list[set[RadioSearchField]] = []
    for term in terms:
        term_fields: set[RadioSearchField] = set()
        for field in fields:
            words = [word for value in document[field] for word in value.split()]
            similarity = max(
                (SequenceMatcher(None, term, word).ratio() for word in words),
                default=0.0,
            )
            if similarity >= FUZZY_MATCH_THRESHOLD:
                term_fields.add(field)
        matched_by_term.append(term_fields)
    if term_match == "all" and not all(matched_by_term):
        return set()
    if term_match == "any" and not any(matched_by_term):
        return set()
    return set().union(*matched_by_term)


def _score(fields: set[RadioSearchField], bonus: int) -> int:
    return max((RADIO_FIELD_WEIGHTS[field] for field in fields), default=0) + bonus


def _normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    unaccented = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(unaccented.casefold().replace("_", " ").split())


def _normalize_code(value: str) -> str:
    return "".join(char for char in _normalize_text(value) if char.isalnum())


def _category_order(category: RadioCodeCategory) -> int:
    return ("response_code", "ten_code", "situation_code", "phonetic").index(category)


def _natural_code_key(value: str) -> tuple[object, ...]:
    return tuple(int(part) if part.isdigit() else part for part in re.split(r"(\d+)", value.casefold()))
