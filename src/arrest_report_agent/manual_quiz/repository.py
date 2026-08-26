"""PostgreSQL catalogue and bounded durable shuffle rotations for manual quizzes."""

from __future__ import annotations

from collections.abc import Collection
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncConnection

from arrest_report_agent.domain import QuizAnswerMode, QuizDifficulty
from arrest_report_agent.infrastructure.postgres import PostgresClient
from arrest_report_agent.infrastructure.postgres.schema import (
    manual_quiz_options,
    manual_quiz_questions,
    manual_quiz_rotation_entries,
    manual_quiz_rotations,
)

from .models import (
    MVP_ANSWER_MODES,
    QuizOption,
    QuizQuestion,
    QuizQuestionReservation,
)


class ManualQuizRepository:
    """Resolve active questions and rotate each guild/filter pool atomically."""

    def __init__(self, client: PostgresClient) -> None:
        self.client = client

    async def reserve_random_active(
        self,
        guild_id: int,
        *,
        answer_mode: QuizAnswerMode | None = None,
        difficulty: QuizDifficulty | None = None,
    ) -> QuizQuestionReservation | None:
        """Claim one unused question, resetting only after this filtered pool is exhausted."""

        answer_mode_filter = answer_mode.value if answer_mode is not None else "any"
        difficulty_filter = difficulty.value if difficulty is not None else "any"
        async with self.client.transaction() as connection:
            await connection.execute(
                postgresql.insert(manual_quiz_rotations)
                .values(
                    guild_id=guild_id,
                    answer_mode_filter=answer_mode_filter,
                    difficulty_filter=difficulty_filter,
                )
                .on_conflict_do_nothing(
                    index_elements=(
                        manual_quiz_rotations.c.guild_id,
                        manual_quiz_rotations.c.answer_mode_filter,
                        manual_quiz_rotations.c.difficulty_filter,
                    )
                )
            )
            rotation = (
                await connection.execute(
                    sa.select(manual_quiz_rotations)
                    .where(
                        manual_quiz_rotations.c.guild_id == guild_id,
                        manual_quiz_rotations.c.answer_mode_filter == answer_mode_filter,
                        manual_quiz_rotations.c.difficulty_filter == difficulty_filter,
                    )
                    .with_for_update()
                )
            ).mappings().one()
            cycle = int(rotation["cycle"])
            candidate_statement = self._active_question_statement(
                guild_id,
                answer_mode=answer_mode,
                difficulty=difficulty,
            ).where(
                ~sa.exists(
                    sa.select(1).where(
                        manual_quiz_rotation_entries.c.guild_id == guild_id,
                        manual_quiz_rotation_entries.c.answer_mode_filter
                        == answer_mode_filter,
                        manual_quiz_rotation_entries.c.difficulty_filter
                        == difficulty_filter,
                        manual_quiz_rotation_entries.c.cycle == cycle,
                        manual_quiz_rotation_entries.c.question_id
                        == manual_quiz_questions.c.id,
                    )
                )
            )
            row = (
                await connection.execute(candidate_statement.order_by(sa.func.random()).limit(1))
            ).mappings().one_or_none()
            if row is None:
                total = await connection.scalar(
                    sa.select(sa.func.count()).select_from(
                        self._active_question_statement(
                            guild_id,
                            answer_mode=answer_mode,
                            difficulty=difficulty,
                        ).subquery()
                    )
                )
                if not total:
                    return None
                await connection.execute(
                    manual_quiz_rotation_entries.delete().where(
                        manual_quiz_rotation_entries.c.guild_id == guild_id,
                        manual_quiz_rotation_entries.c.answer_mode_filter
                        == answer_mode_filter,
                        manual_quiz_rotation_entries.c.difficulty_filter
                        == difficulty_filter,
                    )
                )
                cycle += 1
                await connection.execute(
                    manual_quiz_rotations.update()
                    .where(
                        manual_quiz_rotations.c.guild_id == guild_id,
                        manual_quiz_rotations.c.answer_mode_filter == answer_mode_filter,
                        manual_quiz_rotations.c.difficulty_filter == difficulty_filter,
                    )
                    .values(cycle=cycle, updated_at=sa.func.now())
                )
                row = (
                    await connection.execute(
                        self._active_question_statement(
                            guild_id,
                            answer_mode=answer_mode,
                            difficulty=difficulty,
                        )
                        .order_by(sa.func.random())
                        .limit(1)
                    )
                ).mappings().one()

            await connection.execute(
                manual_quiz_rotation_entries.insert().values(
                    guild_id=guild_id,
                    answer_mode_filter=answer_mode_filter,
                    difficulty_filter=difficulty_filter,
                    cycle=cycle,
                    question_id=row["id"],
                )
            )
            question = await self._hydrate_question(row, connection=connection)
            return QuizQuestionReservation(
                question=question,
                answer_mode_filter=answer_mode_filter,
                difficulty_filter=difficulty_filter,
                cycle=cycle,
            )

    async def cancel_reservation(
        self,
        guild_id: int,
        reservation: QuizQuestionReservation,
    ) -> None:
        """Return an unpublished claim to the same shuffle cycle."""

        await self.client.execute(
            manual_quiz_rotation_entries.delete().where(
                manual_quiz_rotation_entries.c.guild_id == guild_id,
                manual_quiz_rotation_entries.c.answer_mode_filter
                == reservation.answer_mode_filter,
                manual_quiz_rotation_entries.c.difficulty_filter
                == reservation.difficulty_filter,
                manual_quiz_rotation_entries.c.cycle == reservation.cycle,
                manual_quiz_rotation_entries.c.question_id == reservation.question.id,
            )
        )

    async def random_active(
        self,
        guild_id: int,
        *,
        exclude_question_ids: Collection[UUID] = (),
        answer_mode: QuizAnswerMode | None = None,
        difficulty: QuizDifficulty | None = None,
    ) -> QuizQuestion | None:
        statement = self._active_question_statement(
            guild_id,
            answer_mode=answer_mode,
            difficulty=difficulty,
        )
        if exclude_question_ids:
            statement = statement.where(
                manual_quiz_questions.c.id.not_in(tuple(exclude_question_ids))
            )
        row = await self.client.fetch_one(statement.order_by(sa.func.random()).limit(1))
        if row is None:
            return None
        return await self._hydrate_question(row)

    @staticmethod
    def _active_question_statement(
        guild_id: int,
        *,
        answer_mode: QuizAnswerMode | None = None,
        difficulty: QuizDifficulty | None = None,
    ) -> sa.Select[tuple[object, ...]]:
        statement = sa.select(manual_quiz_questions).where(
            manual_quiz_questions.c.guild_id == guild_id,
            manual_quiz_questions.c.status == "active",
            manual_quiz_questions.c.answer_mode.in_(tuple(mode.value for mode in MVP_ANSWER_MODES)),
        )
        if answer_mode is not None:
            statement = statement.where(manual_quiz_questions.c.answer_mode == answer_mode.value)
        if difficulty is not None:
            statement = statement.where(manual_quiz_questions.c.difficulty == difficulty.value)
        return statement

    async def _hydrate_question(
        self,
        row: sa.RowMapping,
        *,
        connection: AsyncConnection | None = None,
    ) -> QuizQuestion:
        option_rows = await self.client.fetch_all(
            sa.select(manual_quiz_options)
            .where(manual_quiz_options.c.question_id == row["id"])
            .order_by(manual_quiz_options.c.position),
            connection=connection,
        )
        return QuizQuestion.model_validate(
            {
                **row,
                "options": tuple(
                    QuizOption.model_validate(
                        {
                            "id": option["id"],
                            "position": option["position"],
                            "text": option["text"],
                            "is_correct": option["is_correct"],
                        }
                    )
                    for option in option_rows
                ),
            }
        )
