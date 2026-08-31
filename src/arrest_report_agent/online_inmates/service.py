"""Short-lived, coalesced access to SARP's public online-character snapshot."""

from __future__ import annotations

import asyncio
import logging
import unicodedata
from time import monotonic
from typing import Protocol

from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.sarp import SarpError, SarpOnlineCharacter

from .models import OnlineInmateSnapshot

LOGGER = logging.getLogger(__name__)


class OnlineCharacterApiClient(Protocol):
    async def get_online_characters(self) -> list[SarpOnlineCharacter]: ...


class OnlineInmateService:
    """Cache the public snapshot and project only connected inmates."""

    def __init__(
        self,
        client: OnlineCharacterApiClient,
        *,
        cache_seconds: float = 30.0,
    ) -> None:
        if cache_seconds < 0:
            raise ValueError("Online-character cache duration cannot be negative")
        self.client = client
        self.cache_seconds = cache_seconds
        self._cache: tuple[float, tuple[SarpOnlineCharacter, ...]] | None = None
        self._inflight: asyncio.Task[list[SarpOnlineCharacter]] | None = None
        self._lock = asyncio.Lock()

    async def query(self) -> OnlineInmateSnapshot:
        characters = await self._online_characters()
        inmate_names = tuple(
            sorted(
                (
                    _display_name(character.character_name)
                    for character in characters
                    if character.character_jailed
                ),
                key=_sort_key,
            )
        )
        return OnlineInmateSnapshot(
            server_connected_count=len(characters),
            inmate_names=inmate_names,
        )

    async def _online_characters(self) -> tuple[SarpOnlineCharacter, ...]:
        now = monotonic()
        stale: tuple[SarpOnlineCharacter, ...] | None = None
        async with self._lock:
            cached = self._cache
            if cached is not None:
                stale = cached[1]
            if cached is not None and cached[0] > now:
                return cached[1]
            task = self._inflight
            if task is None:
                task = asyncio.create_task(
                    self.client.get_online_characters(),
                    name="sarp-online-characters",
                )
                self._inflight = task
        try:
            characters = tuple(await asyncio.shield(task))
        except SarpError as error:
            log_event(
                LOGGER,
                "warning",
                "online_characters_unavailable",
                stale_snapshot_available=stale is not None,
                **exception_fields(error),
            )
            if stale is not None:
                return stale
            raise
        finally:
            if task.done():
                async with self._lock:
                    if self._inflight is task:
                        self._inflight = None
        if self.cache_seconds > 0:
            async with self._lock:
                self._cache = (monotonic() + self.cache_seconds, characters)
        return characters


def _display_name(value: str) -> str:
    return " ".join(value.replace("_", " ").split())


def _sort_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(character for character in normalized if not unicodedata.combining(character))
