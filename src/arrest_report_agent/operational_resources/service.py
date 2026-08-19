"""Cached application service for the operational resources panel."""

from __future__ import annotations

import asyncio
import logging
from time import monotonic
from typing import Protocol

from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.faction_status import FactionStatusService
from arrest_report_agent.infrastructure.sarp import SarpError, SarpServerCacheInfo

from .models import OperationalResourcesSnapshot

LOGGER = logging.getLogger(__name__)
DEFAULT_SERVER_CACHE_SECONDS = 300.0


class OperationalResourcesApiClient(Protocol):
    async def get_server_cache_info(self) -> SarpServerCacheInfo: ...


class OperationalResourcesService:
    """Combine reusable live faction data with a short-lived cache artifact."""

    def __init__(
        self,
        faction_status: FactionStatusService,
        client: OperationalResourcesApiClient,
        *,
        server_cache_seconds: float = DEFAULT_SERVER_CACHE_SECONDS,
    ) -> None:
        if server_cache_seconds < 0:
            raise ValueError("Server cache duration cannot be negative")
        self.faction_status = faction_status
        self.client = client
        self.server_cache_seconds = server_cache_seconds
        self._cache: tuple[float, SarpServerCacheInfo] | None = None
        self._inflight: asyncio.Task[SarpServerCacheInfo] | None = None
        self._lock = asyncio.Lock()

    async def query(self, faction_id: int) -> OperationalResourcesSnapshot:
        status, cache = await asyncio.gather(
            self.faction_status.query(faction_id),
            self._server_cache(),
        )
        return OperationalResourcesSnapshot(
            faction_status=status,
            server_cache=cache if cache is not None and cache.exists else None,
        )

    async def _server_cache(self) -> SarpServerCacheInfo | None:
        now = monotonic()
        stale: SarpServerCacheInfo | None = None
        async with self._lock:
            cached = self._cache
            if cached is not None:
                stale = cached[1]
            if cached is not None and cached[0] > now:
                return cached[1]
            task = self._inflight
            if task is None:
                task = asyncio.create_task(
                    self.client.get_server_cache_info(),
                    name="sarp-server-cache-info",
                )
                self._inflight = task
        try:
            cache = await asyncio.shield(task)
        except SarpError as error:
            log_event(
                LOGGER,
                "warning",
                "server_cache_info_unavailable",
                stale_cache_available=stale is not None,
                **exception_fields(error),
            )
            return stale
        finally:
            if task.done():
                async with self._lock:
                    if self._inflight is task:
                        self._inflight = None
        if self.server_cache_seconds > 0:
            async with self._lock:
                self._cache = (monotonic() + self.server_cache_seconds, cache)
        return cache
