"""Validated in-memory index over the downloaded PCU charge catalog."""

from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
from importlib.resources import files
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


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
        limit: int = 40,
    ) -> dict[str, object]:
        """Return category counts or a compact filtered charge list."""

        bounded_limit = max(1, min(limit, 100))
        selected_category_ids = set(category_ids or [])
        if not query and not selected_category_ids:
            return {
                "categories": [
                    {"id": category.id, "title": category.title, "charge_count": len(category.charges)}
                    for category in self.categories
                ],
                "total_charges": self.charge_count,
            }

        normalized_query = _normalize(query or "")
        query_terms = normalized_query.split()
        matches: list[tuple[int, Charge, ChargeCategory]] = []
        for charge in self._charges_by_id.values():
            category = self._categories_by_id[charge.category]
            if selected_category_ids and category.id not in selected_category_ids:
                continue
            haystack = _normalize(f"{charge.code} {charge.charge} {charge.type} {category.title}")
            if query_terms and not all(term in haystack for term in query_terms):
                continue
            score = _match_score(normalized_query, charge, category)
            matches.append((score, charge, category))
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
                }
                for _, charge, category in matches[:bounded_limit]
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


def _match_score(query: str, charge: Charge, category: ChargeCategory) -> int:
    if not query:
        return 0
    code = _normalize(charge.code)
    title = _normalize(charge.charge)
    category_title = _normalize(category.title)
    if query == code:
        return 100
    if title.startswith(query):
        return 80
    if query in title:
        return 60
    if query in code:
        return 50
    if query in category_title:
        return 20
    return 10


def _strip_html(value: str) -> str:
    with_breaks = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    without_tags = re.sub(r"<[^>]+>", "", with_breaks)
    lines = [" ".join(line.split()) for line in html.unescape(without_tags).splitlines()]
    return "\n".join(line for line in lines if line)
