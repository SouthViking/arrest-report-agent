"""Bounded quiz sessions backed by durable guild/filter shuffle rotations."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from time import monotonic
from uuid import UUID

from .models import (
    ActiveQuizSessionError,
    NoQuizQuestionsError,
    QuizAnswerMode,
    QuizCooldownError,
    QuizDifficulty,
    QuizQuestion,
    QuizQuestionReservation,
)
from .repository import ManualQuizRepository

QUIZ_COOLDOWN_SECONDS = 30


class ManualQuizSessionRegistry:
    """Prevent concurrent questions while the repository owns durable rotation state."""

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
        self._active: dict[tuple[int, int], QuizQuestionReservation] = {}
        self._last_started: dict[tuple[int, int], float] = {}
        self._lock = asyncio.Lock()

    async def reserve(
        self,
        guild_id: int,
        user_id: int,
        *,
        answer_mode: QuizAnswerMode | None = None,
        difficulty: QuizDifficulty | None = None,
    ) -> QuizQuestion:
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

            reservation = await self.repository.reserve_random_active(
                guild_id,
                answer_mode=answer_mode,
                difficulty=difficulty,
            )
            if reservation is None:
                raise NoQuizQuestionsError

            self._active[key] = reservation
            self._last_started[key] = now
            return reservation.question

    async def release(self, guild_id: int, user_id: int, question_id: UUID) -> None:
        key = (guild_id, user_id)
        async with self._lock:
            active = self._active.get(key)
            if active is not None and active.question.id == question_id:
                self._active.pop(key, None)

    async def cancel(self, guild_id: int, user_id: int, question_id: UUID) -> None:
        """Undo a reservation when Discord could not publish its message."""

        key = (guild_id, user_id)
        async with self._lock:
            reservation = self._active.get(key)
            if reservation is None or reservation.question.id != question_id:
                return
            self._active.pop(key, None)
            self._last_started.pop(key, None)
            await self.repository.cancel_reservation(guild_id, reservation)

    async def is_active(self, guild_id: int, user_id: int) -> bool:
        async with self._lock:
            return (guild_id, user_id) in self._active
