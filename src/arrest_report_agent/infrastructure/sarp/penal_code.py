"""Refreshing SARP-backed charge catalog with an immutable local fallback."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Protocol

from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.knowledge import (
    ChargeCatalog,
    ChargeCatalogSnapshot,
    ChargeCatalogSource,
)
from arrest_report_agent.knowledge.catalog import Charge, ChargeCategory

from .models import SarpPenalCodePayload

LOGGER = logging.getLogger(__name__)
DEFAULT_REFRESH_SECONDS = 3600


class PenalCodeClient(Protocol):
    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def get_penal_code(self) -> SarpPenalCodePayload: ...


class SarpPenalCodeProvider:
    """Atomically refresh the shared catalog without making consumers perform I/O."""

    def __init__(
        self,
        client: PenalCodeClient,
        local_catalog: ChargeCatalog,
        *,
        refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
    ) -> None:
        if refresh_seconds <= 0:
            raise ValueError("SARP penal-code refresh interval must be positive")
        self.client = client
        self.local_catalog = local_catalog
        self.refresh_seconds = refresh_seconds
        self._snapshot = ChargeCatalogSnapshot(
            catalog=local_catalog,
            source=ChargeCatalogSource.LOCAL,
        )
        self._last_remote_catalog: ChargeCatalog | None = None
        self._refresh_lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    def snapshot(self) -> ChargeCatalogSnapshot:
        return self._snapshot

    async def start(self) -> None:
        if self._refresh_task is not None:
            return
        await self.client.open()
        await self.refresh()
        self._stopped.clear()
        self._refresh_task = asyncio.create_task(
            self._refresh_loop(),
            name="sarp-penal-code-refresh",
        )

    async def close(self) -> None:
        self._stopped.set()
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            await asyncio.gather(self._refresh_task, return_exceptions=True)
            self._refresh_task = None
        await self.client.close()

    async def refresh(self) -> ChargeCatalogSnapshot:
        async with self._refresh_lock:
            try:
                payload = await self.client.get_penal_code()
                catalog = build_charge_catalog(payload)
            except Exception as error:
                if self._last_remote_catalog is not None:
                    self._snapshot = ChargeCatalogSnapshot(
                        catalog=self._last_remote_catalog,
                        source=ChargeCatalogSource.SARP_CACHED,
                    )
                else:
                    self._snapshot = ChargeCatalogSnapshot(
                        catalog=self.local_catalog,
                        source=ChargeCatalogSource.LOCAL,
                    )
                log_event(
                    LOGGER,
                    "warning",
                    "sarp_penal_code_refresh_failed",
                    exc_info=error,
                    active_source=self._snapshot.source.value,
                    charge_count=self._snapshot.catalog.charge_count,
                    **exception_fields(error),
                )
                return self._snapshot

            self._last_remote_catalog = catalog
            self._snapshot = ChargeCatalogSnapshot(
                catalog=catalog,
                source=ChargeCatalogSource.SARP,
            )
            log_event(
                LOGGER,
                "info",
                "sarp_penal_code_refreshed",
                active_source=self._snapshot.source.value,
                charge_count=catalog.charge_count,
                source_hash=catalog.source_hash,
            )
            return self._snapshot

    async def _refresh_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                await asyncio.wait_for(
                    self._stopped.wait(),
                    timeout=self.refresh_seconds,
                )
                return
            except TimeoutError:
                await self.refresh()


def build_charge_catalog(payload: SarpPenalCodePayload) -> ChargeCatalog:
    """Adapt the flat SARP payload to the existing shared catalog contract."""

    charges_by_category: dict[int, list[Charge]] = {
        category.id: [] for category in payload.categories
    }
    for charge in payload.charges:
        charges_by_category[charge.category].append(
            Charge(
                id=charge.id,
                category=charge.category,
                code=charge.code,
                charge=charge.charge,
                type=charge.type,
                description=charge.description,
                hours=charge.hours if charge.hours is not None else charge.time,
                parole=charge.parole,
                fine=charge.fine,
                points=charge.points,
            )
        )
    categories = [
        ChargeCategory(
            id=category.id,
            title=category.title,
            description=category.description,
            charges=charges_by_category[category.id],
        )
        for category in payload.categories
    ]
    canonical = json.dumps(
        payload.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return ChargeCatalog(categories, hashlib.sha256(canonical).hexdigest())
