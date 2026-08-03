"""Concurrency-safe global Discord presence for review activity."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum

import discord

from arrest_report_agent.config.logging import exception_fields, log_event

LOGGER = logging.getLogger(__name__)


class PresenceMode(StrEnum):
    IDLE = "idle"
    BUSY = "busy"


class ReviewPresence:
    """Publish presence only when the global active-review state changes."""

    def __init__(self, bot: discord.Bot, *, idle_text: str, busy_text: str) -> None:
        self.bot = bot
        self._activity_text = {
            PresenceMode.IDLE: idle_text,
            PresenceMode.BUSY: busy_text,
        }
        self._active_reviews = 0
        self._published_mode: PresenceMode | None = None
        self._lock = asyncio.Lock()

    @property
    def active_reviews(self) -> int:
        return self._active_reviews

    @property
    def mode(self) -> PresenceMode:
        return PresenceMode.BUSY if self._active_reviews else PresenceMode.IDLE

    async def synchronize(self) -> None:
        """Republish the current mode after startup or a Gateway reconnect."""

        async with self._lock:
            await self._publish(self.mode, force=True)

    @asynccontextmanager
    async def reviewing(self) -> AsyncIterator[None]:
        """Track one active review and restore idle after the last review exits."""

        await self._enter_review()
        try:
            yield
        finally:
            await self._leave_review()

    async def _enter_review(self) -> None:
        async with self._lock:
            self._active_reviews += 1
            try:
                await self._publish(self.mode)
            except BaseException:
                self._active_reviews -= 1
                raise

    async def _leave_review(self) -> None:
        async with self._lock:
            self._active_reviews = max(0, self._active_reviews - 1)
            await self._publish(self.mode)

    async def _publish(self, mode: PresenceMode, *, force: bool = False) -> None:
        if not force and mode is self._published_mode:
            return
        activity_text = self._activity_text[mode]
        try:
            await self.bot.change_presence(
                status=discord.Status.online,
                activity=discord.CustomActivity(name=activity_text),
            )
        except Exception as error:
            log_event(
                LOGGER,
                "warning",
                "discord_presence_update_failed",
                exc_info=error,
                active_reviews=self._active_reviews,
                presence_mode=mode.value,
                **exception_fields(error),
            )
            return
        self._published_mode = mode
        log_event(
            LOGGER,
            "info",
            "discord_presence_updated",
            active_reviews=self._active_reviews,
            presence_mode=mode.value,
        )
