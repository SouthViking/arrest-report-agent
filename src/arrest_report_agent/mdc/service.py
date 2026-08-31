"""Parallel, partially fault-tolerant MDC lookup orchestration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from time import monotonic
from typing import Protocol, TypeVar, cast

from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.sarp import (
    SarpArrestPage,
    SarpCharacterPossessions,
    SarpCharacterPreview,
    SarpCharacterSearchResult,
    SarpFinePage,
    SarpWarrantPage,
)
from arrest_report_agent.infrastructure.sarp.character_resolution import (
    CharacterAmbiguousError,
    CharacterNotFoundError,
    resolve_exact_character,
)

from .models import (
    MdcArrest,
    MdcCharacterAmbiguousError,
    MdcCharacterNotFoundError,
    MdcCharge,
    MdcFine,
    MdcProperty,
    MdcRecord,
    MdcSection,
    MdcVehicle,
    MdcWarrant,
    character_name_key,
    normalize_character_name,
)

LOGGER = logging.getLogger(__name__)
DEFAULT_CACHE_SECONDS = 30.0
T = TypeVar("T")


class MdcApiClient(Protocol):
    async def search_characters(self, name: str) -> list[SarpCharacterSearchResult]: ...

    async def get_character_preview(self, character_id: int) -> SarpCharacterPreview: ...

    async def get_character_possessions(
        self,
        character_id: int,
    ) -> SarpCharacterPossessions: ...

    async def list_unpaid_fines(
        self,
        character_id: int,
        *,
        page: int = 1,
        limit: int = 100,
    ) -> SarpFinePage: ...

    async def list_active_warrants(
        self,
        character_id: int,
        *,
        page: int = 1,
        limit: int = 25,
    ) -> SarpWarrantPage: ...

    async def list_arrests(
        self,
        character_id: int,
        *,
        page: int = 1,
        limit: int = 3,
    ) -> SarpArrestPage: ...


@dataclass(frozen=True, slots=True)
class _SectionResult:
    section: MdcSection
    value: object | None
    error: Exception | None


class MdcService:
    """Resolve one exact character and assemble independent IC-safe sections."""

    def __init__(self, client: MdcApiClient, *, cache_seconds: float = DEFAULT_CACHE_SECONDS) -> None:
        if cache_seconds < 0:
            raise ValueError("MDC cache duration cannot be negative")
        self.client = client
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, MdcRecord]] = {}
        self._inflight: dict[str, asyncio.Task[MdcRecord]] = {}
        self._lock = asyncio.Lock()

    async def lookup_exact(self, raw_name: str) -> MdcRecord:
        display_name = normalize_character_name(raw_name)
        if not display_name or len(display_name) > 100:
            raise MdcCharacterNotFoundError("Character name is empty or too long")
        key = character_name_key(display_name)
        now = monotonic()
        async with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached[0] > now:
                return cached[1]
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(
                    self._lookup(display_name),
                    name="mdc-character-lookup",
                )
                self._inflight[key] = task
        try:
            record = await asyncio.shield(task)
        finally:
            if task.done():
                async with self._lock:
                    if self._inflight.get(key) is task:
                        self._inflight.pop(key, None)
        if self.cache_seconds > 0:
            async with self._lock:
                self._cache[key] = (monotonic() + self.cache_seconds, record)
        return record

    async def _lookup(self, display_name: str) -> MdcRecord:
        try:
            character = await resolve_exact_character(self.client, display_name)
        except CharacterNotFoundError as error:
            raise MdcCharacterNotFoundError(display_name) from error
        except CharacterAmbiguousError as error:
            raise MdcCharacterAmbiguousError(display_name) from error
        preview, possessions, fines, warrants, arrests = await asyncio.gather(
            self._capture(
                MdcSection.STATUS,
                self.client.get_character_preview(character.id),
            ),
            self._capture(
                MdcSection.POSSESSIONS,
                self.client.get_character_possessions(character.id),
            ),
            self._capture(
                MdcSection.FINES,
                self.client.list_unpaid_fines(character.id),
            ),
            self._capture(
                MdcSection.WARRANTS,
                self.client.list_active_warrants(character.id),
            ),
            self._capture(
                MdcSection.ARRESTS,
                self.client.list_arrests(character.id),
            ),
        )
        unavailable = tuple(
            result.section
            for result in (preview, possessions, fines, warrants, arrests)
            if result.error is not None
        )
        return _build_record(
            character,
            preview=cast(SarpCharacterPreview | None, preview.value),
            possessions=cast(SarpCharacterPossessions | None, possessions.value),
            fines=cast(SarpFinePage | None, fines.value),
            warrants=cast(SarpWarrantPage | None, warrants.value),
            arrests=cast(SarpArrestPage | None, arrests.value),
            unavailable=unavailable,
        )

    async def _capture(self, section: MdcSection, operation: Awaitable[T]) -> _SectionResult:
        try:
            return _SectionResult(section, await operation, None)
        except Exception as error:
            log_event(
                LOGGER,
                "warning",
                "mdc_section_unavailable",
                section=section.value,
                exc_info=error,
                **exception_fields(error),
            )
            return _SectionResult(section, None, error)


def _build_record(
    character: SarpCharacterSearchResult,
    *,
    preview: SarpCharacterPreview | None,
    possessions: SarpCharacterPossessions | None,
    fines: SarpFinePage | None,
    warrants: SarpWarrantPage | None,
    arrests: SarpArrestPage | None,
    unavailable: tuple[MdcSection, ...],
) -> MdcRecord:
    vehicles = tuple(
        MdcVehicle(
            name=_first_text(vehicle.model_name, vehicle.name, _model_fallback(vehicle.model)),
            plate=_optional_text(vehicle.plate),
        )
        for vehicle in (possessions.vehicles if possessions is not None else ())
    )
    properties = tuple(
        MdcProperty(
            name=_optional_text(property_.name),
            address=_optional_text(property_.address),
        )
        for property_ in (possessions.properties if possessions is not None else ())
    )
    pending_fines = tuple(
        MdcFine(
            description=fine.fine,
            cost=fine.cost,
            created_at=fine.created_at,
        )
        for fine in (fines.items[:3] if fines is not None else ())
    )
    active_warrants = tuple(
        MdcWarrant(
            description=warrant.description,
            types=warrant.types,
            risk=warrant.risk,
            charges=tuple(
                MdcCharge(code=charge.code, name=charge.charge)
                for charge in warrant.charges
            ),
            created_at=warrant.created_at,
        )
        for warrant in (warrants.items if warrants is not None else ())
        if warrant.active
    )
    arrest_entries = tuple(
        MdcArrest(
            charges=tuple(
                MdcCharge(code=charge.code, name=charge.charge)
                for charge in arrest.charges
            ),
            created_at=arrest.created_at,
        )
        for arrest in (arrests.items if arrests is not None else ())
    )
    fine_amount = (
        sum(fine.cost for fine in fines.items)
        if fines is not None and fines.pages <= 1
        else None
    )
    active_warrant_count = None
    if warrants is not None:
        active_warrant_count = (
            warrants.total
            if all(warrant.active for warrant in warrants.items)
            else len(active_warrants)
        )
    return MdcRecord(
        name=normalize_character_name(character.name),
        incarcerated=bool(preview.jailed) if preview is not None else None,
        vehicles=vehicles,
        properties=properties,
        pending_fines=pending_fines,
        pending_fine_count=fines.total if fines is not None else None,
        pending_fine_amount=fine_amount,
        active_warrants=active_warrants[:3],
        active_warrant_count=active_warrant_count,
        arrests=arrest_entries,
        arrest_count=arrests.total if arrests is not None else None,
        unavailable_sections=unavailable,
    )


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized or normalized.casefold() == "null":
        return None
    return normalized


def _first_text(*values: object) -> str:
    for value in values:
        normalized = _optional_text(value)
        if normalized is not None:
            return normalized
    return "Sin información"


def _model_fallback(value: int | str | None) -> str | None:
    return f"Modelo {value}" if value is not None else None
