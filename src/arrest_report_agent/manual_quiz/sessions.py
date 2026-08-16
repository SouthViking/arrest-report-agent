"""Bounded in-memory quiz sessions without user analytics or durable attempts."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from time import monotonic
from uuid import UUID

from .models import (
    ActiveQuizSessionError,
    NoQuizQuestionsError,
    QuizCooldownError,
    QuizQuestion,
)
from .repository import ManualQuizRepository

QUIZ_COOLDOWN_SECONDS = 30


class ManualQuizSessionRegistry:
    """Prevent concurrent questions and avoid repeats until a user's pool is exhausted."""

    def __init__(
        self,
        repository: ManualQuizRepository,
        *,
        cooldown_seconds: int = QUIZ_COOLDOWN_SECONDS,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.repository = repository
        self.cooldown_seconds = cooldown_seconds
        self.clock = clock
        self._active: dict[tuple[int, int], UUID] = {}
        self._seen: dict[tuple[int, int], set[UUID]] = {}
        self._last_started: dict[tuple[int, int], float] = {}
        self._lock = asyncio.Lock()

    async def reserve(self, guild_id: int, user_id: int) -> QuizQuestion:
        key = (guild_id, user_id)
        async with self._lock:
            if key in self._active:
                raise ActiveQuizSessionError
            now = self.clock()
            last_started = self._last_started.get(key)
            if last_started is not None:
                elapsed = now - last_started
                if elapsed < self.cooldown_seconds:
                    raise QuizCooldownError(max(1, math.ceil(self.cooldown_seconds - elapsed)))

            seen = self._seen.setdefault(key, set())
            question = await self.repository.random_active(
                guild_id,
                exclude_question_ids=seen,
            )
            if question is None and seen:
                seen.clear()
                question = await self.repository.random_active(guild_id)
            if question is None:
                raise NoQuizQuestionsError

            self._active[key] = question.id
            seen.add(question.id)
            self._last_started[key] = now
            return question

    async def release(self, guild_id: int, user_id: int, question_id: UUID) -> None:
        key = (guild_id, user_id)
        async with self._lock:
            if self._active.get(key) == question_id:
                self._active.pop(key, None)

    async def cancel(self, guild_id: int, user_id: int, question_id: UUID) -> None:
        """Undo a reservation when Discord could not publish its message."""

        key = (guild_id, user_id)
        async with self._lock:
            if self._active.get(key) != question_id:
                return
            self._active.pop(key, None)
            self._seen.get(key, set()).discard(question_id)
            self._last_started.pop(key, None)

    async def is_active(self, guild_id: int, user_id: int) -> bool:
        async with self._lock:
            return (guild_id, user_id) in self._active
