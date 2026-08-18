"""Short-lived, coalesced access to the live SARP faction roster."""

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
    SarpFactionRank,
    SarpGlobalMetrics,
)

from .models import FactionPresenceMember, FactionPresenceSnapshot

DEFAULT_CACHE_SECONDS = 15.0
DEFAULT_RANK_CACHE_SECONDS = 300.0
DEFAULT_GLOBAL_METRICS_CACHE_SECONDS = 60.0
WHITESPACE = re.compile(r"\s+")
UNKNOWN_RANK = "Rango desconocido"
LOGGER = logging.getLogger(__name__)


class FactionStatusApiClient(Protocol):
    async def get_faction_detail(self, faction_id: int) -> SarpFactionDetail: ...

    async def get_faction_ranks(self, faction_id: int) -> list[SarpFactionRank]: ...

    async def get_global_metrics(self) -> SarpGlobalMetrics: ...


class FactionStatusService:
    """Read and filter one roster without multiplying SARP requests per command."""

    def __init__(
        self,
        client: FactionStatusApiClient,
        *,
        cache_seconds: float = DEFAULT_CACHE_SECONDS,
        rank_cache_seconds: float = DEFAULT_RANK_CACHE_SECONDS,
        global_metrics_cache_seconds: float = DEFAULT_GLOBAL_METRICS_CACHE_SECONDS,
    ) -> None:
        if cache_seconds < 0:
            raise ValueError("Faction status cache duration cannot be negative")
        if rank_cache_seconds < 0:
            raise ValueError("Faction rank cache duration cannot be negative")
        if global_metrics_cache_seconds < 0:
            raise ValueError("Global metrics cache duration cannot be negative")
        self.client = client
        self.cache_seconds = cache_seconds
        self.rank_cache_seconds = rank_cache_seconds
        self.global_metrics_cache_seconds = global_metrics_cache_seconds
        self._cache: dict[int, tuple[float, SarpFactionDetail]] = {}
        self._inflight: dict[int, asyncio.Task[SarpFactionDetail]] = {}
        self._rank_cache: dict[int, tuple[float, tuple[SarpFactionRank, ...]]] = {}
        self._rank_inflight: dict[int, asyncio.Task[list[SarpFactionRank]]] = {}
        self._global_metrics_cache: tuple[float, SarpGlobalMetrics] | None = None
        self._global_metrics_inflight: asyncio.Task[SarpGlobalMetrics] | None = None
        self._lock = asyncio.Lock()

    async def query(self, faction_id: int) -> FactionPresenceSnapshot:
        detail, ranks, global_metrics = await asyncio.gather(
            self._detail(faction_id),
            self._ranks(faction_id),
            self._global_metrics(),
        )
        selected = (
            FactionPresenceMember(
                name=_display_name(member.name),
                on_duty=member.factionduty,
                rank_name=_rank_name(member.rank, ranks),
                rank_order=_rank_order(member.rank, ranks),
            )
            for member in detail.faction_members
            if member.online
        )
        connected_members = tuple(
            sorted(
                selected,
                key=lambda member: (-member.rank_order, _alphabetical_key(member.name)),
            )
        )
        return FactionPresenceSnapshot(
            faction_id=faction_id,
            total_members=len(detail.faction_members),
            connected_members=connected_members,
            server_connected_count=(
                global_metrics.online_account_counter if global_metrics is not None else None
            ),
        )

    async def _global_metrics(self) -> SarpGlobalMetrics | None:
        now = monotonic()
        stale: SarpGlobalMetrics | None = None
        async with self._lock:
            cached = self._global_metrics_cache
            if cached is not None:
                stale = cached[1]
            if cached is not None and cached[0] > now:
                return cached[1]
            task = self._global_metrics_inflight
            if task is None:
                task = asyncio.create_task(
                    self.client.get_global_metrics(),
                    name="sarp-global-metrics",
                )
                self._global_metrics_inflight = task
        try:
            metrics = await asyncio.shield(task)
        except SarpError as error:
            log_event(
                LOGGER,
                "warning",
                "global_metrics_unavailable",
                stale_metrics_available=stale is not None,
                **exception_fields(error),
            )
            return stale
        finally:
            if task.done():
                async with self._lock:
                    if self._global_metrics_inflight is task:
                        self._global_metrics_inflight = None
        if self.global_metrics_cache_seconds > 0:
            async with self._lock:
                self._global_metrics_cache = (
                    monotonic() + self.global_metrics_cache_seconds,
                    metrics,
                )
        return metrics

    async def _detail(self, faction_id: int) -> SarpFactionDetail:
        now = monotonic()
        async with self._lock:
            cached = self._cache.get(faction_id)
            if cached is not None and cached[0] > now:
                return cached[1]
            task = self._inflight.get(faction_id)
            if task is None:
                task = asyncio.create_task(
                    self.client.get_faction_detail(faction_id),
                    name=f"sarp-faction-status-{faction_id}",
                )
                self._inflight[faction_id] = task
        try:
            detail = await asyncio.shield(task)
        finally:
            if task.done():
                async with self._lock:
                    if self._inflight.get(faction_id) is task:
                        self._inflight.pop(faction_id, None)
        if self.cache_seconds > 0:
            async with self._lock:
                self._cache[faction_id] = (monotonic() + self.cache_seconds, detail)
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


def _display_name(value: str) -> str:
    return WHITESPACE.sub(" ", value.replace("_", " ").strip())


def _alphabetical_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(character for character in normalized if not unicodedata.combining(character))


def _rank_order(rank: int | None, ranks: tuple[SarpFactionRank, ...]) -> int:
    if rank is None or not 1 <= rank <= len(ranks):
        return -1
    return rank


def _rank_name(rank: int | None, ranks: tuple[SarpFactionRank, ...]) -> str:
    order = _rank_order(rank, ranks)
    return ranks[order - 1].name if order >= 1 else UNKNOWN_RANK
