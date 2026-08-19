"""Short-lived, coalesced access to the live SARP faction roster."""

from __future__ import annotations

import asyncio
import logging
from time import monotonic
from typing import Protocol

from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.faction_directory import FactionDirectoryService
from arrest_report_agent.infrastructure.sarp import (
    SarpError,
    SarpGlobalMetrics,
)

from .models import FactionPresenceMember, FactionPresenceSnapshot

DEFAULT_GLOBAL_METRICS_CACHE_SECONDS = 60.0
LOGGER = logging.getLogger(__name__)


class FactionStatusApiClient(Protocol):
    async def get_global_metrics(self) -> SarpGlobalMetrics: ...


class FactionStatusService:
    """Read and filter one roster without multiplying SARP requests per command."""

    def __init__(
        self,
        client: FactionStatusApiClient,
        *,
        directory: FactionDirectoryService | None = None,
        cache_seconds: float = 15.0,
        rank_cache_seconds: float = 300.0,
        global_metrics_cache_seconds: float = DEFAULT_GLOBAL_METRICS_CACHE_SECONDS,
    ) -> None:
        if cache_seconds < 0:
            raise ValueError("Faction status cache duration cannot be negative")
        if rank_cache_seconds < 0:
            raise ValueError("Faction rank cache duration cannot be negative")
        if global_metrics_cache_seconds < 0:
            raise ValueError("Global metrics cache duration cannot be negative")
        self.client = client
        self.directory = directory or FactionDirectoryService(
            client,  # type: ignore[arg-type]
            roster_cache_seconds=cache_seconds,
            rank_cache_seconds=rank_cache_seconds,
        )
        self.global_metrics_cache_seconds = global_metrics_cache_seconds
        self._global_metrics_cache: tuple[float, SarpGlobalMetrics] | None = None
        self._global_metrics_inflight: asyncio.Task[SarpGlobalMetrics] | None = None
        self._lock = asyncio.Lock()

    async def query(self, faction_id: int) -> FactionPresenceSnapshot:
        directory, global_metrics = await asyncio.gather(
            self.directory.snapshot(faction_id),
            self._global_metrics(),
        )
        selected = (
            FactionPresenceMember(
                name=member.name,
                on_duty=member.on_duty,
                rank_name=member.rank_name,
                rank_order=member.rank_order,
            )
            for member in directory.members
            if member.online
        )
        connected_members = tuple(selected)
        return FactionPresenceSnapshot(
            faction_id=faction_id,
            total_members=len(directory.members),
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
