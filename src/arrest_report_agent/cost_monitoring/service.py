"""Concurrent, cached orchestration for cost providers."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Protocol, TypeVar

from arrest_report_agent.config import AppSettings
from arrest_report_agent.config.logging import exception_fields, log_event

from .models import CostMonitoringSnapshot, OpenAICostSnapshot, RailwayCostSnapshot
from .providers import OpenAICostProvider, RailwayCostProvider

LOGGER = logging.getLogger(__name__)
SnapshotT = TypeVar("SnapshotT", covariant=True)


class CostProvider(Protocol[SnapshotT]):
    async def fetch(self) -> SnapshotT: ...

    async def close(self) -> None: ...


class CostMonitoringUnavailableError(LookupError):
    """No provider returned enough information to render a summary."""


class CostMonitoringService:
    """Fetch both providers without letting one failure hide the other."""

    def __init__(
        self,
        *,
        openai: CostProvider[OpenAICostSnapshot] | None,
        railway: CostProvider[RailwayCostSnapshot] | None,
        cache_seconds: int,
    ) -> None:
        self.openai = openai
        self.railway = railway
        self.cache_seconds = cache_seconds
        self._cached: CostMonitoringSnapshot | None = None
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return self.openai is not None or self.railway is not None

    async def snapshot(self) -> CostMonitoringSnapshot:
        async with self._lock:
            now = datetime.now(UTC)
            if self._cached is not None:
                age = (now - self._cached.fetched_at).total_seconds()
                if age < self.cache_seconds:
                    return self._cached

            openai_result, railway_result = await asyncio.gather(
                self._fetch_openai(),
                self._fetch_railway(),
            )
            snapshot = CostMonitoringSnapshot(
                fetched_at=now,
                openai=openai_result,
                railway=railway_result,
            )
            if not snapshot.has_data:
                raise CostMonitoringUnavailableError(
                    "No fue posible consultar el consumo de los proveedores configurados."
                )
            self._cached = snapshot
            return snapshot

    async def _fetch_openai(self) -> OpenAICostSnapshot | None:
        return await self._fetch("openai", self.openai)

    async def _fetch_railway(self) -> RailwayCostSnapshot | None:
        return await self._fetch("railway", self.railway)

    @staticmethod
    async def _fetch(
        provider_name: str,
        provider: CostProvider[SnapshotT] | None,
    ) -> SnapshotT | None:
        if provider is None:
            return None
        try:
            return await provider.fetch()
        except Exception as error:
            log_event(
                LOGGER,
                "warning",
                "cost_monitor_provider_failed",
                provider=provider_name,
                **exception_fields(error),
            )
            return None

    async def close(self) -> None:
        providers = [provider for provider in (self.openai, self.railway) if provider is not None]
        await asyncio.gather(*(provider.close() for provider in providers), return_exceptions=True)


def build_cost_monitoring_service(settings: AppSettings) -> CostMonitoringService | None:
    """Create only the providers with complete process credentials."""

    openai = (
        OpenAICostProvider(
            settings.openai_admin_key.get_secret_value(),
            project_id=settings.openai_project_id,
            timeout_seconds=settings.cost_monitor_timeout_seconds,
        )
        if settings.openai_cost_monitoring_enabled and settings.openai_admin_key is not None
        else None
    )
    railway = (
        RailwayCostProvider(
            settings.railway_api_token.get_secret_value(),
            settings.railway_workspace_id,
            timeout_seconds=settings.cost_monitor_timeout_seconds,
        )
        if settings.railway_cost_monitoring_enabled
        and settings.railway_api_token is not None
        and settings.railway_workspace_id is not None
        else None
    )
    if openai is None and railway is None:
        return None
    return CostMonitoringService(
        openai=openai,
        railway=railway,
        cache_seconds=settings.cost_monitor_cache_seconds,
    )
