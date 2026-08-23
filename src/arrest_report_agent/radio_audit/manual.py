"""Bounded local search over the communication section of the faction manual."""

from __future__ import annotations

import json
import unicodedata
from importlib.resources import files
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RadioManualSection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(pattern=r"^[a-z][a-z0-9_]+$")
    title: str = Field(min_length=1, max_length=160)
    aliases: tuple[str, ...] = ()
    content: tuple[str, ...] = Field(min_length=1)


class RadioManualPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(min_length=1, max_length=32)
    source: str = Field(min_length=1, max_length=300)
    sections: tuple[RadioManualSection, ...] = Field(min_length=1)


class RadioManualCatalog:
    """Search only radio-use rules, never unrelated manual chapters."""

    def __init__(self, payload: RadioManualPayload) -> None:
        if len({section.key for section in payload.sections}) != len(payload.sections):
            raise ValueError("radio manual section keys must be unique")
        self.version = payload.version
        self.source = payload.source
        self.sections = payload.sections
        self._documents = {
            section.key: _normalize(" ".join((section.title, *section.aliases, *section.content)))
            for section in payload.sections
        }

    @classmethod
    def load_default(cls) -> RadioManualCatalog:
        resource = files("arrest_report_agent.knowledge").joinpath("data/radio_manual.json")
        return cls(RadioManualPayload.model_validate(json.loads(resource.read_text(encoding="utf-8"))))

    def list_sections(self) -> dict[str, object]:
        """Return a compact discovery index without loading section content."""

        return {
            "version": self.version,
            "source": self.source,
            "sections": [
                {
                    "key": section.key,
                    "title": section.title,
                    "aliases": list(section.aliases),
                }
                for section in self.sections
            ],
        }

    def search(
        self,
        queries: list[str],
        *,
        section_keys: list[str] | None = None,
        term_match: Literal["all", "any"] = "all",
        limit: int = 8,
    ) -> dict[str, object]:
        normalized_queries = tuple(
            dict.fromkeys(normalized for query in queries if (normalized := _normalize(query)))
        )
        if not normalized_queries:
            raise ValueError("at least one radio manual query is required")
        if len(normalized_queries) > 6:
            raise ValueError("at most six radio manual queries are allowed")
        if term_match not in {"all", "any"}:
            raise ValueError("term_match must be 'all' or 'any'")
        requested_keys = tuple(
            dict.fromkeys(_normalize_key(key) for key in section_keys or () if key.strip())
        )
        if len(requested_keys) > 8:
            raise ValueError("at most eight radio manual section keys are allowed")
        available_keys = {section.key for section in self.sections}
        selected_keys = tuple(key for key in requested_keys if key in available_keys)
        unknown_keys = tuple(key for key in requested_keys if key not in available_keys)
        selected_key_set = set(selected_keys)
        matches: list[tuple[int, RadioManualSection]] = []
        for section in self.sections:
            if requested_keys and section.key not in selected_key_set:
                continue
            document = self._documents[section.key]
            score = 0
            for query in normalized_queries:
                terms = query.split()
                matched_terms = sum(term in document for term in terms)
                accepted = matched_terms == len(terms) if term_match == "all" else matched_terms > 0
                if not accepted:
                    continue
                score += 100 if query == _normalize(section.title) else 20 * matched_terms
            if score:
                matches.append((score, section))
        matches.sort(key=lambda item: (-item[0], item[1].title.casefold()))
        bounded = max(1, min(limit, 12))
        return {
            "version": self.version,
            "source": self.source,
            "matched": len(matches),
            "returned": min(len(matches), bounded),
            "selected_section_keys": list(selected_keys),
            "unknown_section_keys": list(unknown_keys),
            "sections": [
                {
                    "key": section.key,
                    "title": section.title,
                    "content": list(section.content),
                }
                for _, section in matches[:bounded]
            ],
        }

    def resolve_reference(self, value: str) -> str | None:
        """Resolve a model-provided key or title to one canonical section title."""

        normalized = _normalize(value)
        for section in self.sections:
            if normalized in {_normalize(section.key), _normalize(section.title)}:
                return section.title
        return None


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    unaccented = "".join(character for character in decomposed if not unicodedata.combining(character))
    return " ".join(unaccented.casefold().replace("_", " ").split())


def _normalize_key(value: str) -> str:
    return "_".join(_normalize(value.replace("-", " ")).split())
