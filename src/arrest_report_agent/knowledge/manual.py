"""Validated, navigable catalog over the complete packaged faction manual."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import date
from difflib import SequenceMatcher
from importlib.resources import files
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ManualMatchMode = Literal["all", "any", "phrase"]
ManualReadDirection = Literal["forward", "backward"]

FUZZY_MIN_TERM_LENGTH = 4
FUZZY_MATCH_THRESHOLD = 0.82
MAX_SEARCH_QUERIES = 5
MAX_SECTION_FILTERS = 10
MAX_SEARCH_LIMIT = 30
MAX_LIST_LIMIT = 50
MAX_READ_CHARS = 6_000
MAX_EXCERPT_CHARS = 700


class ManualSection(BaseModel):
    """One ordered section and its direct content, excluding child sections."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,99}$")
    title: str = Field(min_length=1, max_length=300)
    level: int = Field(ge=1, le=6)
    parent_id: str | None = Field(default=None, max_length=100)
    order: int = Field(ge=1)
    content: tuple[str, ...] = Field(default_factory=tuple)
    aliases: tuple[str, ...] = Field(default_factory=tuple, max_length=12)

    @field_validator("content", "aliases")
    @classmethod
    def reject_blank_items(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("manual section lists cannot contain blank items")
        return values


class ManualPayload(BaseModel):
    """Versioned local snapshot extracted from the official faction manual."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(min_length=1, max_length=32)
    source: str = Field(min_length=1, max_length=256)
    source_url: str = Field(min_length=1, max_length=500)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    captured_at: date
    sections: tuple[ManualSection, ...]


@dataclass(slots=True)
class _SearchMatch:
    score: int
    offset: int
    matched_queries: list[str] = dataclass_field(default_factory=list)
    match_kinds: set[str] = dataclass_field(default_factory=set)


@dataclass(frozen=True, slots=True)
class _SearchDocument:
    section: ManualSection
    path: tuple[str, ...]
    text: str
    normalized_title: str
    normalized_path: str
    paragraphs: tuple[tuple[int, str, str], ...]


class ManualCatalog:
    """Search and scroll through a bounded, immutable faction-manual snapshot."""

    def __init__(self, payload: ManualPayload, source_hash: str) -> None:
        if not payload.sections:
            raise ValueError("Manual catalog must contain at least one section")
        ids = [section.id for section in payload.sections]
        orders = [section.order for section in payload.sections]
        if len(ids) != len(set(ids)):
            raise ValueError("Manual section IDs must be unique")
        if len(orders) != len(set(orders)):
            raise ValueError("Manual section orders must be unique")
        if orders != sorted(orders):
            raise ValueError("Manual sections must be sorted by order")

        self.version = payload.version
        self.source = payload.source
        self.source_url = payload.source_url
        self.source_sha256 = payload.source_sha256
        self.captured_at = payload.captured_at
        self.source_hash = source_hash
        self.sections = payload.sections
        self._by_id = {section.id: section for section in self.sections}
        self._children: dict[str | None, tuple[ManualSection, ...]] = {}
        mutable_children: dict[str | None, list[ManualSection]] = {}
        for section in self.sections:
            parent = self._by_id.get(section.parent_id) if section.parent_id is not None else None
            if section.parent_id is not None and parent is None:
                raise ValueError(f"Unknown manual parent section: {section.parent_id}")
            if parent is not None and (parent.order >= section.order or parent.level >= section.level):
                raise ValueError(f"Invalid manual hierarchy for section: {section.id}")
            mutable_children.setdefault(section.parent_id, []).append(section)
        self._children = {
            parent_id: tuple(children) for parent_id, children in mutable_children.items()
        }
        self._documents = {
            section.id: self._build_document(section) for section in self.sections
        }

    @classmethod
    def load_default(cls) -> ManualCatalog:
        resource = files("arrest_report_agent.knowledge").joinpath("data/lssd_manual.json")
        return cls.from_path(Path(str(resource)))

    @classmethod
    def from_path(cls, path: Path) -> ManualCatalog:
        raw = path.read_bytes()
        payload = ManualPayload.model_validate(json.loads(raw))
        return cls(payload, hashlib.sha256(raw).hexdigest())

    @property
    def section_count(self) -> int:
        return len(self.sections)

    def list_sections(
        self,
        *,
        parent_id: str | None = None,
        include_descendants: bool = False,
        query: str | None = None,
        level: int | None = None,
        offset: int = 0,
        limit: int = 25,
    ) -> dict[str, object]:
        """List a compact hierarchy view with deterministic offset pagination."""

        if parent_id is not None and parent_id not in self._by_id:
            return self._not_found(parent_id)
        bounded_offset = max(0, offset)
        bounded_limit = max(1, min(limit, MAX_LIST_LIMIT))
        if query is not None:
            normalized_query = _normalize_text(query)
            if not normalized_query:
                return self._invalid_request("query cannot be blank")
            candidates = self._scoped_sections(parent_id, include_descendants=True)
            query_terms = normalized_query.split()
            candidates = tuple(
                section
                for section in candidates
                if all(
                    term
                    in _normalize_text(
                        " ".join(
                            (
                                section.title,
                                *section.aliases,
                                *self._documents[section.id].path,
                            )
                        )
                    )
                    for term in query_terms
                )
            )
        else:
            candidates = self._scoped_sections(parent_id, include_descendants)
        if level is not None:
            candidates = tuple(section for section in candidates if section.level == level)

        page = candidates[bounded_offset : bounded_offset + bounded_limit]
        next_offset = bounded_offset + len(page)
        return {
            "ok": True,
            "version": self.version,
            "source": self.source,
            "parent_id": parent_id,
            "results": [self._section_summary(section) for section in page],
            "matched": len(candidates),
            "returned": len(page),
            "offset": bounded_offset,
            "next_offset": next_offset if next_offset < len(candidates) else None,
        }

    def search(
        self,
        queries: list[str],
        *,
        section_ids: list[str] | None = None,
        include_descendants: bool = True,
        match_mode: ManualMatchMode = "all",
        fuzzy: bool = False,
        offset: int = 0,
        limit: int = 10,
    ) -> dict[str, object]:
        """Search independent queries across selected sections and return compact excerpts."""

        normalized_queries = _deduplicate_queries(queries)
        if not normalized_queries:
            return self._invalid_request("at least one non-empty query is required")
        if len(normalized_queries) > MAX_SEARCH_QUERIES:
            return self._invalid_request(f"at most {MAX_SEARCH_QUERIES} queries are allowed")
        if match_mode not in {"all", "any", "phrase"}:
            return self._invalid_request("match_mode must be 'all', 'any' or 'phrase'")

        selected_ids = tuple(dict.fromkeys(section_ids or ()))
        if len(selected_ids) > MAX_SECTION_FILTERS:
            return self._invalid_request(
                f"at most {MAX_SECTION_FILTERS} section filters are allowed"
            )
        unknown_ids = [section_id for section_id in selected_ids if section_id not in self._by_id]
        if unknown_ids:
            return {
                "ok": False,
                "error": "section_not_found",
                "unknown_section_ids": unknown_ids,
            }

        if selected_ids:
            allowed_ids: set[str] = set()
            for section_id in selected_ids:
                allowed_ids.add(section_id)
                if include_descendants:
                    allowed_ids.update(section.id for section in self._descendants(section_id))
            candidates = tuple(
                section for section in self.sections if section.id in allowed_ids
            )
        else:
            candidates = self.sections

        matches: dict[str, _SearchMatch] = {}
        unmatched_queries: list[str] = []
        for original_query, normalized_query in normalized_queries:
            found_for_query = False
            for section in candidates:
                document = self._documents[section.id]
                result = _match_document(
                    document,
                    normalized_query,
                    match_mode=match_mode,
                    fuzzy=fuzzy,
                )
                if result is None:
                    continue
                found_for_query = True
                score, match_offset, match_kind = result
                current = matches.get(section.id)
                if current is None:
                    current = _SearchMatch(score=score, offset=match_offset)
                    matches[section.id] = current
                elif score > current.score:
                    current.score = score
                    current.offset = match_offset
                current.matched_queries.append(original_query)
                current.match_kinds.add(match_kind)
            if not found_for_query:
                unmatched_queries.append(original_query)

        ordered = sorted(
            matches.items(),
            key=lambda item: (-item[1].score, self._by_id[item[0]].order),
        )
        bounded_offset = max(0, offset)
        bounded_limit = max(1, min(limit, MAX_SEARCH_LIMIT))
        page = ordered[bounded_offset : bounded_offset + bounded_limit]
        results: list[dict[str, object]] = []
        for section_id, match in page:
            document = self._documents[section_id]
            excerpt, excerpt_offset = _excerpt(document.text, match.offset)
            results.append(
                {
                    **self._section_summary(document.section),
                    "excerpt": excerpt,
                    "match_offset": excerpt_offset,
                    "matched_queries": match.matched_queries,
                    "match_kinds": sorted(match.match_kinds),
                }
            )
        next_offset = bounded_offset + len(page)
        return {
            "ok": True,
            "version": self.version,
            "source": self.source,
            "results": results,
            "matched": len(ordered),
            "returned": len(results),
            "offset": bounded_offset,
            "next_offset": next_offset if next_offset < len(ordered) else None,
            "unmatched_queries": unmatched_queries,
        }

    def read(
        self,
        section_id: str,
        *,
        offset: int = 0,
        max_chars: int = 3_000,
        direction: ManualReadDirection = "forward",
    ) -> dict[str, object]:
        """Read one section from an offset in either direction, like bounded document scrolling."""

        document = self._documents.get(section_id)
        if document is None:
            return self._not_found(section_id)
        bounded_chars = max(500, min(max_chars, MAX_READ_CHARS))
        text_length = len(document.text)
        bounded_offset = max(0, min(offset, text_length))
        if direction == "forward":
            start = bounded_offset
            end = min(text_length, start + bounded_chars)
        else:
            end = bounded_offset or text_length
            start = max(0, end - bounded_chars)

        children = self._children.get(section_id, ())
        return {
            "ok": True,
            "version": self.version,
            "source": self.source,
            **self._section_summary(document.section),
            "text": document.text[start:end],
            "start_offset": start,
            "end_offset": end,
            "total_chars": text_length,
            "has_more_before": start > 0,
            "has_more_after": end < text_length,
            "previous_offset": start if start > 0 else None,
            "next_offset": end if end < text_length else None,
            "children": [self._section_summary(child) for child in children],
        }

    def _build_document(self, section: ManualSection) -> _SearchDocument:
        path = self._path(section)
        text = "\n\n".join(section.content)
        paragraphs: list[tuple[int, str, str]] = []
        offset = 0
        for paragraph in section.content:
            paragraphs.append((offset, paragraph, _normalize_text(paragraph)))
            offset += len(paragraph) + 2
        return _SearchDocument(
            section=section,
            path=path,
            text=text,
            normalized_title=_normalize_text(" ".join((section.title, *section.aliases))),
            normalized_path=_normalize_text(" > ".join(path)),
            paragraphs=tuple(paragraphs),
        )

    def _path(self, section: ManualSection) -> tuple[str, ...]:
        titles = [section.title]
        parent_id = section.parent_id
        while parent_id is not None:
            parent = self._by_id[parent_id]
            titles.append(parent.title)
            parent_id = parent.parent_id
        return tuple(reversed(titles))

    def _scoped_sections(
        self,
        parent_id: str | None,
        include_descendants: bool,
    ) -> tuple[ManualSection, ...]:
        if parent_id is None:
            return self.sections if include_descendants else self._children.get(None, ())
        if include_descendants:
            return self._descendants(parent_id)
        return self._children.get(parent_id, ())

    def _descendants(self, parent_id: str) -> tuple[ManualSection, ...]:
        descendants: list[ManualSection] = []
        pending = list(self._children.get(parent_id, ()))
        while pending:
            section = pending.pop(0)
            descendants.append(section)
            pending[0:0] = self._children.get(section.id, ())
        return tuple(descendants)

    def _section_summary(self, section: ManualSection) -> dict[str, object]:
        document = self._documents[section.id]
        return {
            "section_id": section.id,
            "title": section.title,
            "path": list(document.path),
            "level": section.level,
            "parent_id": section.parent_id,
            "order": section.order,
            "has_content": bool(section.content),
            "child_count": len(self._children.get(section.id, ())),
        }

    @staticmethod
    def _invalid_request(message: str) -> dict[str, object]:
        return {"ok": False, "error": "invalid_request", "message": message}

    @staticmethod
    def _not_found(section_id: str) -> dict[str, object]:
        return {"ok": False, "error": "section_not_found", "section_id": section_id}


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


def _match_document(
    document: _SearchDocument,
    query: str,
    *,
    match_mode: ManualMatchMode,
    fuzzy: bool,
) -> tuple[int, int, str] | None:
    terms = query.split()
    if query == document.normalized_title:
        return 130, 0, "exact_title"
    if query in document.normalized_title:
        return 115, 0, "title_phrase"
    if _terms_match(terms, document.normalized_title, match_mode):
        return 100, 0, "title_terms"
    if fuzzy and _fuzzy_terms_match(terms, document.normalized_title, match_mode):
        return 95, 0, "title_fuzzy"
    if query in document.normalized_path:
        return 90, 0, "path_phrase"
    if fuzzy and _fuzzy_terms_match(terms, document.normalized_path, match_mode):
        return 85, 0, "path_fuzzy"

    normalized_document = " ".join(
        (
            document.normalized_title,
            document.normalized_path,
            *(paragraph[2] for paragraph in document.paragraphs),
        )
    )
    best: tuple[int, int, str] | None = None
    if match_mode == "all" and _terms_match(terms, normalized_document, match_mode):
        match_offset = next(
            (
                paragraph_offset
                for paragraph_offset, _paragraph, normalized_paragraph in document.paragraphs
                if any(term in normalized_paragraph for term in terms)
            ),
            0,
        )
        best = (70, match_offset, "section_terms")
    elif (
        fuzzy
        and match_mode == "all"
        and _fuzzy_terms_match(terms, normalized_document, match_mode)
    ):
        best = (45, 0, "section_fuzzy")

    for offset, _paragraph, normalized_paragraph in document.paragraphs:
        if query in normalized_paragraph:
            candidate = (80, offset, "content_phrase")
        elif _terms_match(terms, normalized_paragraph, match_mode):
            matched_terms = sum(term in normalized_paragraph for term in terms)
            candidate = (55 + matched_terms, offset, "content_terms")
        elif fuzzy and _fuzzy_terms_match(terms, normalized_paragraph, match_mode):
            candidate = (35, offset, "content_fuzzy")
        else:
            continue
        if best is None or candidate[0] > best[0]:
            best = candidate
    return best


def _terms_match(terms: list[str], document: str, mode: ManualMatchMode) -> bool:
    if not terms:
        return False
    if mode == "phrase":
        return " ".join(terms) in document
    matches = [term in document for term in terms]
    return all(matches) if mode == "all" else any(matches)


def _fuzzy_terms_match(terms: list[str], document: str, mode: ManualMatchMode) -> bool:
    words = set(document.split())
    if not words or not terms:
        return False
    matches = [
        term in words
        or (
            len(term) >= FUZZY_MIN_TERM_LENGTH
            and any(
                SequenceMatcher(None, term, word).ratio() >= FUZZY_MATCH_THRESHOLD
                for word in words
                if abs(len(term) - len(word)) <= 2
            )
        )
        for term in terms
    ]
    return all(matches) if mode in {"all", "phrase"} else any(matches)


def _excerpt(text: str, offset: int) -> tuple[str, int]:
    if len(text) <= MAX_EXCERPT_CHARS:
        return text, 0
    start = max(0, offset - 160)
    end = min(len(text), start + MAX_EXCERPT_CHARS)
    if end - start < MAX_EXCERPT_CHARS:
        start = max(0, end - MAX_EXCERPT_CHARS)
    excerpt = text[start:end].strip()
    if start > 0:
        excerpt = f"…{excerpt}"
    if end < len(text):
        excerpt = f"{excerpt}…"
    return excerpt, start


def _normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", without_marks)).strip()
