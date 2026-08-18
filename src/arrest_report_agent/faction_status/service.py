"""Short-lived, coalesced access to the live SARP faction roster."""

from __future__ import annotations

import asyncio
import re
import unicodedata
from time import monotonic
from typing import Protocol

from arrest_report_agent.infrastructure.sarp import SarpFactionDetail

from .models import FactionPresenceMember, FactionPresenceSnapshot

DEFAULT_CACHE_SECONDS = 15.0
WHITESPACE = re.compile(r"\s+")


class FactionStatusApiClient(Protocol):
    async def get_faction_detail(self, faction_id: int) -> SarpFactionDetail: ...


class FactionStatusService:
    """Read and filter one roster without multiplying SARP requests per command."""

    def __init__(
        self,
        client: FactionStatusApiClient,
        *,
        cache_seconds: float = DEFAULT_CACHE_SECONDS,
    ) -> None:
        if cache_seconds < 0:
            raise ValueError("Faction status cache duration cannot be negative")
        self.client = client
        self.cache_seconds = cache_seconds
        self._cache: dict[int, tuple[float, SarpFactionDetail]] = {}
        self._inflight: dict[int, asyncio.Task[SarpFactionDetail]] = {}
        self._lock = asyncio.Lock()

    async def query(self, faction_id: int) -> FactionPresenceSnapshot:
        detail = await self._detail(faction_id)
        selected = (
            FactionPresenceMember(
                name=_display_name(member.name),
                on_duty=member.factionduty,
            )
            for member in detail.faction_members
            if member.online
        )
        connected_members = tuple(
            sorted(
                selected,
                key=lambda member: _alphabetical_key(member.name),
            )
        )
        return FactionPresenceSnapshot(
            faction_id=faction_id,
            total_members=len(detail.faction_members),
            connected_members=connected_members,
        )

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


def _display_name(value: str) -> str:
    return WHITESPACE.sub(" ", value.replace("_", " ").strip())


def _alphabetical_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(character for character in normalized if not unicodedata.combining(character))
