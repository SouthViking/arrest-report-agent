"""Read-only PostgreSQL catalogue access for manual quiz questions."""

from __future__ import annotations

from collections.abc import Collection
from uuid import UUID

import sqlalchemy as sa

from arrest_report_agent.infrastructure.postgres import PostgresClient
from arrest_report_agent.infrastructure.postgres.schema import (
    manual_quiz_options,
    manual_quiz_questions,
)

from .models import (
    MVP_ANSWER_MODES,
    QuizAnswerMode,
    QuizDifficulty,
    QuizOption,
    QuizQuestion,
)


class ManualQuizRepository:
    """Resolve validated active questions without leaking SQL into Discord code."""

    def __init__(self, client: PostgresClient) -> None:
        self.client = client

    async def random_active(
        self,
        guild_id: int,
        *,
        exclude_question_ids: Collection[UUID] = (),
        answer_mode: QuizAnswerMode | None = None,
        difficulty: QuizDifficulty | None = None,
    ) -> QuizQuestion | None:
        statement = sa.select(manual_quiz_questions).where(
            manual_quiz_questions.c.guild_id == guild_id,
            manual_quiz_questions.c.status == "active",
            manual_quiz_questions.c.answer_mode.in_(tuple(mode.value for mode in MVP_ANSWER_MODES)),
        )
        if answer_mode is not None:
            statement = statement.where(manual_quiz_questions.c.answer_mode == answer_mode.value)
        if difficulty is not None:
            statement = statement.where(manual_quiz_questions.c.difficulty == difficulty.value)
        if exclude_question_ids:
            statement = statement.where(manual_quiz_questions.c.id.not_in(tuple(exclude_question_ids)))
        row = await self.client.fetch_one(statement.order_by(sa.func.random()).limit(1))
        if row is None:
            return None
        option_rows = await self.client.fetch_all(
            sa.select(manual_quiz_options)
            .where(manual_quiz_options.c.question_id == row["id"])
            .order_by(manual_quiz_options.c.position)
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
