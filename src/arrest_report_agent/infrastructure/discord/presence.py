"""Concurrency-safe global Discord presence for agent activity."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum

import discord

from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.domain import AgentActivity

LOGGER = logging.getLogger(__name__)


class PresenceMode(StrEnum):
    IDLE = "idle"
    CONVERSATION = AgentActivity.CONVERSATION
    KNOWLEDGE = AgentActivity.KNOWLEDGE
    PERSONNEL = AgentActivity.PERSONNEL
    RECORDS = AgentActivity.RECORDS
    EVIDENCE = AgentActivity.EVIDENCE
    REVIEW = AgentActivity.REVIEW
    RESPONSE = AgentActivity.RESPONSE


PRESENCE_PRIORITY = {
    PresenceMode.IDLE: 0,
    PresenceMode.CONVERSATION: 1,
    PresenceMode.PERSONNEL: 2,
    PresenceMode.KNOWLEDGE: 3,
    PresenceMode.RECORDS: 4,
    PresenceMode.EVIDENCE: 5,
    PresenceMode.REVIEW: 6,
    PresenceMode.RESPONSE: 7,
}


class PresenceLease:
    """One in-flight interaction registered in the global presence controller."""

    def __init__(self, owner: ReviewPresence, token: int) -> None:
        self._owner = owner
        self._token = token
        self._closed = False

    async def transition(self, activity: AgentActivity) -> None:
        if self._closed:
            return
        await self._owner._transition(self._token, PresenceMode(activity.value))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._owner._release(self._token)


class ReviewPresence:
    """Publish the highest-priority activity across all concurrent interactions."""

    def __init__(
        self,
        bot: discord.Bot,
        *,
        idle_text: str,
        conversation_text: str,
        knowledge_text: str,
        personnel_text: str,
        records_text: str,
        evidence_text: str,
        review_text: str,
        response_text: str,
    ) -> None:
        self.bot = bot
        self._activity_text = {
            PresenceMode.IDLE: idle_text,
            PresenceMode.CONVERSATION: conversation_text,
            PresenceMode.KNOWLEDGE: knowledge_text,
            PresenceMode.PERSONNEL: personnel_text,
            PresenceMode.RECORDS: records_text,
            PresenceMode.EVIDENCE: evidence_text,
            PresenceMode.REVIEW: review_text,
            PresenceMode.RESPONSE: response_text,
        }
        self._active: dict[int, PresenceMode] = {}
        self._next_token = 0
        self._published_mode: PresenceMode | None = None
        self._lock = asyncio.Lock()

    @property
    def active_requests(self) -> int:
        return len(self._active)

    @property
    def mode(self) -> PresenceMode:
        return max(
            self._active.values(),
            key=PRESENCE_PRIORITY.__getitem__,
            default=PresenceMode.IDLE,
        )

    async def synchronize(self) -> None:
        """Republish the current mode after startup or a Gateway reconnect."""

        async with self._lock:
            await self._publish(self.mode, force=True)

    async def update_texts(
        self,
        *,
        idle_text: str,
        conversation_text: str,
        knowledge_text: str,
        personnel_text: str,
        records_text: str,
        evidence_text: str,
        review_text: str,
        response_text: str,
    ) -> None:
        async with self._lock:
            self._activity_text = {
                PresenceMode.IDLE: idle_text,
                PresenceMode.CONVERSATION: conversation_text,
                PresenceMode.KNOWLEDGE: knowledge_text,
                PresenceMode.PERSONNEL: personnel_text,
                PresenceMode.RECORDS: records_text,
                PresenceMode.EVIDENCE: evidence_text,
                PresenceMode.REVIEW: review_text,
                PresenceMode.RESPONSE: response_text,
            }
            await self._publish(self.mode, force=True)

    @asynccontextmanager
    async def handling(self) -> AsyncIterator[PresenceLease]:
        """Track an eligible interaction until its complete runner lifecycle exits."""

        lease = await self._register(PresenceMode.CONVERSATION)
        try:
            yield lease
        finally:
            await lease.close()

    async def _register(self, mode: PresenceMode) -> PresenceLease:
        async with self._lock:
            self._next_token += 1
            token = self._next_token
            self._active[token] = mode
            await self._publish(self.mode)
            return PresenceLease(self, token)

    async def _transition(self, token: int, mode: PresenceMode) -> None:
        async with self._lock:
            current = self._active.get(token)
            if current is None or current is mode:
                return
            self._active[token] = mode
            await self._publish(self.mode)

    async def _release(self, token: int) -> None:
        async with self._lock:
            self._active.pop(token, None)
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
                active_requests=self.active_requests,
                presence_mode=mode.value,
                **exception_fields(error),
            )
            return
        self._published_mode = mode
        log_event(
            LOGGER,
            "info",
            "discord_presence_updated",
            active_requests=self.active_requests,
            presence_mode=mode.value,
        )
