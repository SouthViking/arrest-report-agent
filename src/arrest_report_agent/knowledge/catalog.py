"""Validated in-memory index over the downloaded PCU charge catalog."""

from __future__ import annotations

import hashlib
import html
import json
import re
import shlex
import unicodedata
from difflib import SequenceMatcher
from importlib.resources import files
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

ChargeSearchField = Literal["code", "title", "type", "category", "description"]
ChargeMatchMode = Literal["all", "any"]
DEFAULT_SEARCH_FIELDS: tuple[ChargeSearchField, ...] = (
    "code",
    "title",
    "type",
    "category",
)
FIELD_WEIGHTS: dict[ChargeSearchField, int] = {
    "code": 100,
    "title": 80,
    "type": 30,
    "category": 20,
    "description": 15,
}
FUZZY_MIN_TERM_LENGTH = 4
FUZZY_MATCH_THRESHOLD = 0.82


class Charge(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    category: int
    code: str
    charge: str
    type: str
    description: str
    hours: int | float | None = None
    parole: int | float | None = None
    fine: int | float | None = None
    points: int | float | None = None
    updated_at: str


class ChargeCategory(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    title: str
    description: str | None = None
    updated_at: str
    charges: list[Charge] = Field(default_factory=list)


class ChargeCatalog:
    """Search charges by compact index and resolve authoritative details by unique ID."""

    def __init__(self, categories: list[ChargeCategory], source_hash: str) -> None:
        self.categories = tuple(categories)
        self.source_hash = source_hash
        self._categories_by_id = {category.id: category for category in categories}
        charges = [charge for category in categories for charge in category.charges]
        if len({charge.id for charge in charges}) != len(charges):
            raise ValueError("Charge IDs must be globally unique")
        self._charges_by_id = {charge.id: charge for charge in charges}
        self._search_documents = {
            charge.id: _search_document(charge, self._categories_by_id[charge.category]) for charge in charges
        }

    @classmethod
    def load_default(cls) -> ChargeCatalog:
        resource = files("arrest_report_agent.knowledge").joinpath("data/delitos.json")
        return cls.from_path(Path(str(resource)))

    @classmethod
    def from_path(cls, path: Path) -> ChargeCatalog:
        raw = path.read_bytes()
        payload = json.loads(raw)
        categories = TypeAdapter(list[ChargeCategory]).validate_python(payload)
        return cls(categories, hashlib.sha256(raw).hexdigest())

    @property
    def charge_count(self) -> int:
        return len(self._charges_by_id)

    def list_index(
        self,
        category_ids: list[int] | None = None,
        query: str | None = None,
        fields: list[ChargeSearchField] | None = None,
        match_mode: ChargeMatchMode = "all",
        fuzzy: bool = False,
        limit: int = 20,
    ) -> dict[str, object]:
        """Return category counts or a compact filtered charge list."""

        bounded_limit = max(1, min(limit, 100))
        selected_category_ids = set(category_ids or [])
        selected_fields = tuple(dict.fromkeys(fields or DEFAULT_SEARCH_FIELDS))
        if not selected_fields:
            raise ValueError("At least one search field is required")
        unknown_fields = set(selected_fields) - set(FIELD_WEIGHTS)
        if unknown_fields:
            raise ValueError(f"Unknown search fields: {sorted(unknown_fields)}")
        if match_mode not in {"all", "any"}:
            raise ValueError("match_mode must be 'all' or 'any'")
        if not query and not selected_category_ids:
            return {
                "categories": [
                    {"id": category.id, "title": category.title, "charge_count": len(category.charges)}
                    for category in self.categories
                ],
                "total_charges": self.charge_count,
            }

        normalized_query = _normalize(query or "")
        query_terms = _query_terms(query or "")
        matches: list[tuple[int, Charge, ChargeCategory, list[ChargeSearchField]]] = []
        for charge in self._charges_by_id.values():
            category = self._categories_by_id[charge.category]
            if selected_category_ids and category.id not in selected_category_ids:
                continue
            document = self._search_documents[charge.id]
            term_matches = [
                _match_term(
                    term,
                    document,
                    selected_fields,
                    fuzzy=fuzzy,
                )
                for term in query_terms
            ]
            if query_terms and match_mode == "all" and not all(term_matches):
                continue
            if query_terms and match_mode == "any" and not any(term_matches):
                continue
            matched_fields = sorted(
                {field for field_matches in term_matches for field in field_matches},
                key=tuple(FIELD_WEIGHTS).index,
            )
            score = _match_score(
                normalized_query,
                document,
                term_matches,
            )
            matches.append((score, charge, category, matched_fields))
        matches.sort(key=lambda item: (-item[0], item[1].code, item[1].id))
        return {
            "results": [
                {
                    "id": charge.id,
                    "category_id": category.id,
                    "category": category.title,
                    "code": charge.code,
                    "title": charge.charge,
                    "type": charge.type,
                    "matched_fields": matched_fields,
                }
                for _, charge, category, matched_fields in matches[:bounded_limit]
            ],
            "matched": len(matches),
            "returned": min(len(matches), bounded_limit),
        }

    def get_details(self, charge_ids: list[int]) -> dict[str, object]:
        """Resolve up to ten charges by stable unique ID."""

        deduplicated = list(dict.fromkeys(charge_ids))
        if not deduplicated:
            raise ValueError("At least one charge ID is required")
        if len(deduplicated) > 10:
            raise ValueError("At most ten charge IDs can be requested at once")
        found: list[dict[str, object]] = []
        missing: list[int] = []
        for charge_id in deduplicated:
            charge = self._charges_by_id.get(charge_id)
            if charge is None:
                missing.append(charge_id)
                continue
            category = self._categories_by_id[charge.category]
            found.append(
                {
                    "id": charge.id,
                    "category_id": category.id,
                    "category": category.title,
                    "code": charge.code,
                    "title": charge.charge,
                    "type": charge.type,
                    "description": _strip_html(charge.description),
                    "sentence_hours": charge.hours,
                    "parole": charge.parole,
                    "fine": charge.fine,
                    "points": charge.points,
                    "updated_at": charge.updated_at,
                }
            )
        return {"charges": found, "missing_ids": missing}


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    unaccented = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(unaccented.casefold().replace("_", " ").split())


def _query_terms(query: str) -> list[str]:
    """Split ordinary words while preserving intentionally quoted phrases."""

    try:
        raw_terms = shlex.split(query)
    except ValueError:
        raw_terms = query.split()
    return [normalized for term in raw_terms if (normalized := _normalize(term))]


def _search_document(
    charge: Charge,
    category: ChargeCategory,
) -> dict[ChargeSearchField, str]:
    return {
        "code": _normalize(charge.code),
        "title": _normalize(charge.charge),
        "type": _normalize(charge.type),
        "category": _normalize(category.title),
        "description": _normalize(_strip_html(charge.description)),
    }


def _match_term(
    term: str,
    document: dict[ChargeSearchField, str],
    fields: tuple[ChargeSearchField, ...],
    *,
    fuzzy: bool,
) -> dict[ChargeSearchField, int]:
    matches: dict[ChargeSearchField, int] = {}
    for field in fields:
        value = document[field]
        if term == value:
            matches[field] = 30
        elif value.startswith(term):
            matches[field] = 20
        elif term in value:
            matches[field] = 10
        elif fuzzy and len(term) >= FUZZY_MIN_TERM_LENGTH:
            words = value.split()
            similarity = max(
                (SequenceMatcher(None, term, word).ratio() for word in words),
                default=0.0,
            )
            if similarity >= FUZZY_MATCH_THRESHOLD:
                matches[field] = max(1, round(similarity * 8))
    return matches


def _match_score(
    query: str,
    document: dict[ChargeSearchField, str],
    term_matches: list[dict[ChargeSearchField, int]],
) -> int:
    if not query:
        return 0
    score = sum(
        max((FIELD_WEIGHTS[field] + quality for field, quality in matches.items()), default=0)
        for matches in term_matches
    )
    if query == document["code"]:
        score += 1000
    elif query == document["title"]:
        score += 800
    elif document["title"].startswith(query):
        score += 500
    elif query in document["title"]:
        score += 300
    return score


def _strip_html(value: str) -> str:
    with_breaks = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    without_tags = re.sub(r"<[^>]+>", "", with_breaks)
    lines = [" ".join(line.split()) for line in html.unescape(without_tags).splitlines()]
    return "\n".join(line for line in lines if line)
