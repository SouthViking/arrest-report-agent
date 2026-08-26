"""Bounded quiz sessions for durable rotations and transient agent questions."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from uuid import UUID

from arrest_report_agent.domain import QuizAnswerMode, QuizDifficulty

from .models import (
    ActiveQuizSessionError,
    NoQuizQuestionsError,
    QuizCooldownError,
    QuizQuestion,
    QuizQuestionReservation,
)
from .repository import ManualQuizRepository

QUIZ_COOLDOWN_SECONDS = 30


@dataclass(frozen=True, slots=True)
class _ActiveQuizSession:
    question: QuizQuestion
    reservation: QuizQuestionReservation | None = None


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
        self._active: dict[tuple[int, int], _ActiveQuizSession] = {}
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
            now = self.clock()
            self._assert_available(key, now)

            reservation = await self.repository.reserve_random_active(
                guild_id,
                answer_mode=answer_mode,
                difficulty=difficulty,
            )
            if reservation is None:
                raise NoQuizQuestionsError

            self._active[key] = _ActiveQuizSession(
                question=reservation.question,
                reservation=reservation,
            )
            self._last_started[key] = now
            return reservation.question

    async def reserve_dynamic(
        self,
        guild_id: int,
        user_id: int,
        question: QuizQuestion,
    ) -> QuizQuestion:
        """Activate a validated transient question without adding it to the durable pool."""

        if question.guild_id != guild_id:
            raise ValueError("Dynamic quiz question belongs to another guild")
        key = (guild_id, user_id)
        async with self._lock:
            now = self.clock()
            self._assert_available(key, now)
            self._active[key] = _ActiveQuizSession(question=question)
            self._last_started[key] = now
            return question

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
            active = self._active.get(key)
            if active is None or active.question.id != question_id:
                return
            self._active.pop(key, None)
            self._last_started.pop(key, None)
            if active.reservation is not None:
                await self.repository.cancel_reservation(guild_id, active.reservation)

    async def is_active(self, guild_id: int, user_id: int) -> bool:
        async with self._lock:
            return (guild_id, user_id) in self._active

    def _assert_available(self, key: tuple[int, int], now: float) -> None:
        if key in self._active:
            raise ActiveQuizSessionError
        last_started = self._last_started.get(key)
        if last_started is None:
            return
        elapsed = now - last_started
        if elapsed < self.cooldown_seconds:
            raise QuizCooldownError(max(1, math.ceil(self.cooldown_seconds - elapsed)))
