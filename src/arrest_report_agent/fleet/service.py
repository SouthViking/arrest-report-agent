"""Load the complete fleet once; filter, rank and paginate locally."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic
from typing import Protocol

from arrest_report_agent.config.logging import log_event
from arrest_report_agent.infrastructure.sarp.errors import SarpInvalidResponseError
from arrest_report_agent.infrastructure.sarp.models import SarpFactionVehicle, SarpFactionVehiclePage

LOGGER = logging.getLogger(__name__)
MAX_PAGES = 50


class FleetView(StrEnum):
    SUMMARY = "summary"
    HEALTH = "health"
    FUEL = "fuel"
    OIL = "oil"


class AlprFilter(StrEnum):
    ALL = "all"
    EQUIPPED = "equipped"
    NOT_EQUIPPED = "not_equipped"


class FleetClient(Protocol):
    async def list_faction_vehicles(
        self,
        faction_id: int,
        *,
        page: int = 1,
        limit: int = 100,
    ) -> SarpFactionVehiclePage: ...


@dataclass(frozen=True, slots=True)
class FleetSnapshot:
    faction_id: int
    vehicles: tuple[SarpFactionVehicle, ...]
    fetched_at: datetime

    @property
    def models(self) -> tuple[str, ...]:
        return tuple(sorted({vehicle.name for vehicle in self.vehicles}, key=str.casefold))

    def find_plate(self, plate: str) -> tuple[SarpFactionVehicle, ...]:
        key = plate.strip().casefold()
        return tuple(
            vehicle
            for vehicle in self.vehicles
            if key in (vehicle.plate.strip().casefold(), (vehicle.visible_plate or "").strip().casefold())
            and key
        )

    def select(
        self,
        view: FleetView,
        *,
        model: str | None = None,
        alpr: AlprFilter = AlprFilter.ALL,
    ) -> tuple[SarpFactionVehicle, ...]:
        rows = (
            vehicle
            for vehicle in self.vehicles
            if (model is None or vehicle.name == model)
            and (alpr is AlprFilter.ALL or vehicle.alpr is (alpr is AlprFilter.EQUIPPED))
        )
        if view is FleetView.SUMMARY:
            return tuple(sorted(rows, key=lambda v: (v.name.casefold(), v.plate.casefold(), v.id)))
        # Missing metrics belong at the end, never interpreted as zero.
        return tuple(
            sorted(
                rows,
                key=lambda v: (
                    getattr(v, view.value) is None,
                    getattr(v, view.value) or 0,
                    v.plate.casefold(),
                    v.id,
                ),
            )
        )


class FleetService:
    def __init__(self, client: FleetClient) -> None:
        self.client = client
        self._cache: dict[int, tuple[float, FleetSnapshot]] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    async def snapshot(self, faction_id: int, *, cache_seconds: int = 120) -> FleetSnapshot:
        if faction_id <= 0 or not 1 <= cache_seconds <= 3600:
            raise ValueError("Invalid fleet faction or cache duration")
        async with self._locks.setdefault(faction_id, asyncio.Lock()):
            cached = self._cache.get(faction_id)
            if cached is not None and monotonic() - cached[0] < cache_seconds:
                return cached[1]
            snapshot = await self._load(faction_id)
            self._cache[faction_id] = (monotonic(), snapshot)
            return snapshot

    async def _load(self, faction_id: int) -> FleetSnapshot:
        first = await self.client.list_faction_vehicles(faction_id, page=1, limit=100)
        if first.pages > MAX_PAGES:
            raise SarpInvalidResponseError("Fleet exceeds the pagination limit")
        rows: dict[int, SarpFactionVehicle] = {}
        for number in range(1, max(1, first.pages) + 1):
            page = (
                first
                if number == 1
                else await self.client.list_faction_vehicles(
                    faction_id,
                    page=number,
                    limit=100,
                )
            )
            if page.page != number or page.pages != first.pages or page.total != first.total:
                raise SarpInvalidResponseError("Fleet pagination changed during collection; retry required")
            for vehicle in page.items:
                if vehicle.faction != faction_id or vehicle.id in rows:
                    raise SarpInvalidResponseError("Fleet contains foreign or duplicate vehicles")
                rows[vehicle.id] = vehicle
        if len(rows) != first.total:
            raise SarpInvalidResponseError("Incomplete fleet snapshot")
        result = FleetSnapshot(faction_id, tuple(rows.values()), datetime.now(UTC))
        log_event(
            LOGGER,
            "info",
            "fleet_snapshot_loaded",
            faction_id=faction_id,
            vehicle_count=len(rows),
            pages=first.pages,
        )
        return result
