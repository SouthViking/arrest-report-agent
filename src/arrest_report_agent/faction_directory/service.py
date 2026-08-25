"""Coalesced cached access to faction members and their 1-based rank catalog."""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from time import monotonic
from typing import Protocol

from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.sarp import (
    SarpError,
    SarpFactionDetail,
    SarpFactionDivision,
    SarpFactionFrequency,
    SarpFactionMember,
    SarpFactionRank,
)

from .models import FactionDirectoryMember, FactionDirectorySnapshot

DEFAULT_ROSTER_CACHE_SECONDS = 15.0
DEFAULT_RANK_CACHE_SECONDS = 300.0
UNKNOWN_RANK = "Rango desconocido"
WHITESPACE = re.compile(r"\s+")
LOGGER = logging.getLogger(__name__)


class FactionDirectoryApiClient(Protocol):
    async def get_faction_detail(self, faction_id: int) -> SarpFactionDetail: ...

    async def get_faction_ranks(self, faction_id: int) -> list[SarpFactionRank]: ...

    async def get_faction_divisions(self, faction_id: int) -> list[SarpFactionDivision]: ...

    async def get_faction_frequencies(self, faction_id: int) -> list[SarpFactionFrequency]: ...


class FactionDirectoryService:
    """Share one roster/rank cache across commands that inspect a faction."""

    def __init__(
        self,
        client: FactionDirectoryApiClient,
        *,
        roster_cache_seconds: float = DEFAULT_ROSTER_CACHE_SECONDS,
        rank_cache_seconds: float = DEFAULT_RANK_CACHE_SECONDS,
    ) -> None:
        if roster_cache_seconds < 0 or rank_cache_seconds < 0:
            raise ValueError("Faction directory cache durations cannot be negative")
        self.client = client
        self.roster_cache_seconds = roster_cache_seconds
        self.rank_cache_seconds = rank_cache_seconds
        self._roster_cache: dict[int, tuple[float, SarpFactionDetail]] = {}
        self._roster_inflight: dict[int, asyncio.Task[SarpFactionDetail]] = {}
        self._rank_cache: dict[int, tuple[float, tuple[SarpFactionRank, ...]]] = {}
        self._rank_inflight: dict[int, asyncio.Task[list[SarpFactionRank]]] = {}
        self._division_cache: dict[int, tuple[float, tuple[SarpFactionDivision, ...]]] = {}
        self._division_inflight: dict[int, asyncio.Task[list[SarpFactionDivision]]] = {}
        self._frequency_cache: dict[int, tuple[float, tuple[SarpFactionFrequency, ...]]] = {}
        self._frequency_inflight: dict[int, asyncio.Task[list[SarpFactionFrequency]]] = {}
        self._lock = asyncio.Lock()

    async def roster(self, faction_id: int) -> tuple[SarpFactionMember, ...]:
        """Return the cached raw roster without forcing an unrelated rank request."""

        if faction_id <= 0:
            raise ValueError("faction_id must be positive")
        detail = await self._detail(faction_id)
        return tuple(detail.faction_members)

    async def divisions(self, faction_id: int) -> tuple[SarpFactionDivision, ...]:
        """Return the ordered division catalog using the long-lived metadata cache."""

        if faction_id <= 0:
            raise ValueError("faction_id must be positive")
        now = monotonic()
        stale: tuple[SarpFactionDivision, ...] = ()
        async with self._lock:
            cached = self._division_cache.get(faction_id)
            if cached is not None:
                stale = cached[1]
            if cached is not None and cached[0] > now:
                return cached[1]
            task = self._division_inflight.get(faction_id)
            if task is None:
                task = asyncio.create_task(
                    self.client.get_faction_divisions(faction_id),
                    name=f"sarp-faction-divisions-{faction_id}",
                )
                self._division_inflight[faction_id] = task
        try:
            divisions = tuple(await asyncio.shield(task))
        except SarpError as error:
            log_event(
                LOGGER,
                "warning",
                "faction_division_catalog_unavailable",
                faction_id=faction_id,
                stale_catalog_available=bool(stale),
                **exception_fields(error),
            )
            if stale:
                return stale
            raise
        finally:
            if task.done():
                async with self._lock:
                    if self._division_inflight.get(faction_id) is task:
                        self._division_inflight.pop(faction_id, None)
        if self.rank_cache_seconds > 0:
            async with self._lock:
                self._division_cache[faction_id] = (
                    monotonic() + self.rank_cache_seconds,
                    divisions,
                )
        return divisions

    async def frequencies(self, faction_id: int) -> tuple[SarpFactionFrequency, ...]:
        """Return configured frequencies using the shared long-lived metadata cache."""

        if faction_id <= 0:
            raise ValueError("faction_id must be positive")
        now = monotonic()
        stale: tuple[SarpFactionFrequency, ...] = ()
        async with self._lock:
            cached = self._frequency_cache.get(faction_id)
            if cached is not None:
                stale = cached[1]
            if cached is not None and cached[0] > now:
                return cached[1]
            task = self._frequency_inflight.get(faction_id)
            if task is None:
                task = asyncio.create_task(
                    self.client.get_faction_frequencies(faction_id),
                    name=f"sarp-faction-frequencies-{faction_id}",
                )
                self._frequency_inflight[faction_id] = task
        try:
            frequencies = tuple(await asyncio.shield(task))
        except SarpError as error:
            log_event(
                LOGGER,
                "warning",
                "faction_frequency_catalog_unavailable",
                faction_id=faction_id,
                stale_catalog_available=bool(stale),
                **exception_fields(error),
            )
            if stale:
                return stale
            raise
        finally:
            if task.done():
                async with self._lock:
                    if self._frequency_inflight.get(faction_id) is task:
                        self._frequency_inflight.pop(faction_id, None)
        if self.rank_cache_seconds > 0:
            async with self._lock:
                self._frequency_cache[faction_id] = (
                    monotonic() + self.rank_cache_seconds,
                    frequencies,
                )
        return frequencies

    async def snapshot(self, faction_id: int) -> FactionDirectorySnapshot:
        if faction_id <= 0:
            raise ValueError("faction_id must be positive")
        detail, ranks = await asyncio.gather(
            self._detail(faction_id),
            self._ranks(faction_id),
        )
        members = tuple(
            sorted(
                (
                    FactionDirectoryMember(
                        name=display_name(member.name),
                        online=member.online,
                        on_duty=member.factionduty,
                        rank_name=rank_name(member.rank, ranks),
                        rank_order=rank_order(member.rank, ranks),
                        salary=rank_salary(member.rank, ranks),
                        last_connected_at=member.last_connected_at,
                        faction_time_minutes=member.faction_time_minutes,
                    )
                    for member in detail.faction_members
                ),
                key=lambda member: (-member.rank_order, alphabetical_key(member.name)),
            )
        )
        return FactionDirectorySnapshot(faction_id=faction_id, members=members)

    async def _detail(self, faction_id: int) -> SarpFactionDetail:
        now = monotonic()
        async with self._lock:
            cached = self._roster_cache.get(faction_id)
            if cached is not None and cached[0] > now:
                return cached[1]
            task = self._roster_inflight.get(faction_id)
            if task is None:
                task = asyncio.create_task(
                    self.client.get_faction_detail(faction_id),
                    name=f"sarp-faction-directory-{faction_id}",
                )
                self._roster_inflight[faction_id] = task
        try:
            detail = await asyncio.shield(task)
        finally:
            if task.done():
                async with self._lock:
                    if self._roster_inflight.get(faction_id) is task:
                        self._roster_inflight.pop(faction_id, None)
        if self.roster_cache_seconds > 0:
            async with self._lock:
                self._roster_cache[faction_id] = (
                    monotonic() + self.roster_cache_seconds,
                    detail,
                )
        return detail

    async def _ranks(self, faction_id: int) -> tuple[SarpFactionRank, ...]:
        now = monotonic()
        stale: tuple[SarpFactionRank, ...] = ()
        async with self._lock:
            cached = self._rank_cache.get(faction_id)
            if cached is not None:
                stale = cached[1]
            if cached is not None and cached[0] > now:
                return cached[1]
            task = self._rank_inflight.get(faction_id)
            if task is None:
                task = asyncio.create_task(
                    self.client.get_faction_ranks(faction_id),
                    name=f"sarp-faction-ranks-{faction_id}",
                )
                self._rank_inflight[faction_id] = task
        try:
            ranks = tuple(await asyncio.shield(task))
        except SarpError as error:
            log_event(
                LOGGER,
                "warning",
                "faction_rank_catalog_unavailable",
                faction_id=faction_id,
                stale_catalog_available=bool(stale),
                **exception_fields(error),
            )
            return stale
        finally:
            if task.done():
                async with self._lock:
                    if self._rank_inflight.get(faction_id) is task:
                        self._rank_inflight.pop(faction_id, None)
        if self.rank_cache_seconds > 0:
            async with self._lock:
                self._rank_cache[faction_id] = (
                    monotonic() + self.rank_cache_seconds,
                    ranks,
                )
        return ranks


def display_name(value: str) -> str:
    return WHITESPACE.sub(" ", value.replace("_", " ").strip())


def alphabetical_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(character for character in normalized if not unicodedata.combining(character))


def rank_order(rank: int | None, ranks: tuple[SarpFactionRank, ...]) -> int:
    """Return SARP's 1-based hierarchy position without applying an index offset."""

    if rank is None or not 1 <= rank <= len(ranks):
        return -1
    return rank


def rank_name(rank: int | None, ranks: tuple[SarpFactionRank, ...]) -> str:
    order = rank_order(rank, ranks)
    return ranks[order - 1].name if order >= 1 else UNKNOWN_RANK


def rank_salary(rank: int | None, ranks: tuple[SarpFactionRank, ...]) -> int | float | None:
    order = rank_order(rank, ranks)
    return ranks[order - 1].paycheck if order >= 1 else None
