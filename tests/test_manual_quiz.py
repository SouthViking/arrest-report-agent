from __future__ import annotations

import asyncio
import json
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import discord
import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError

from arrest_report_agent.infrastructure.postgres import PostgresClient, PostgresClientConfig
from arrest_report_agent.infrastructure.postgres.schema import (
    discord_guilds,
    manual_quiz_options,
    manual_quiz_questions,
)
from arrest_report_agent.manual_quiz import (
    ActiveQuizSessionError,
    ManualQuizCog,
    ManualQuizRepository,
    ManualQuizSessionRegistry,
    ManualQuizView,
    QuizAnswerMode,
    QuizCategory,
    QuizCooldownError,
    QuizDifficulty,
    QuizOption,
    QuizQuestion,
    QuizQuestionStatus,
)
from arrest_report_agent.manual_quiz.discord import build_question_embed, build_result_embed

SEED_SQL_PATH = Path(__file__).parents[1] / "scripts" / "seed_manual_quiz_questions.sql"


def _seed_payload() -> list[dict[str, object]]:
    sql = SEED_SQL_PATH.read_text(encoding="utf-8")
    serialized = sql.split("$manual_quiz_questions$\n", 1)[1].split(
        "\n$manual_quiz_questions$::jsonb",
        1,
    )[0]
    payload = json.loads(serialized)
    assert isinstance(payload, list)
    return payload


def _question(
    *,
    question_id: UUID | None = None,
    guild_id: int = 1,
    answer_mode: QuizAnswerMode = QuizAnswerMode.SINGLE_CHOICE,
) -> QuizQuestion:
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    return QuizQuestion(
        id=question_id or uuid4(),
        guild_id=guild_id,
        slug="qa_question",
        answer_mode=answer_mode,
        category=QuizCategory.PROCEDURE,
        difficulty=QuizDifficulty.BASIC,
        prompt="¿Cuál es el procedimiento correcto?",
        explanation="La alternativa A corresponde al procedimiento descrito en el manual.",
        source_reference="Manual LSSD · Sección II",
        status=QuizQuestionStatus.ACTIVE,
        revision=1,
        options=(
            QuizOption(id=uuid4(), position=0, text="Alternativa correcta", is_correct=True),
            QuizOption(id=uuid4(), position=1, text="Alternativa incorrecta", is_correct=False),
        ),
        created_at=now,
        updated_at=now,
    )


def _member(user_id: int, role_ids: tuple[int, ...], *, guild_id: int = 1) -> discord.Member:
    roles = {role_id: SimpleNamespace(id=role_id) for role_id in role_ids}
    guild = SimpleNamespace(id=guild_id, get_role=roles.get, default_role=None)
    state = SimpleNamespace(
        store_user=lambda data: SimpleNamespace(
            id=int(data["id"]),
            global_name=None,
            name=data["username"],
            avatar=None,
            default_avatar=SimpleNamespace(url="https://cdn.example/avatar.png"),
        )
    )
    return discord.Member(
        data={
            "user": {"id": str(user_id), "username": f"Member {user_id}"},
            "roles": [str(role_id) for role_id in role_ids],
        },
        guild=guild,
        state=state,
    )


def test_manual_quiz_question_validates_single_choice_and_true_false_contracts() -> None:
    single = _question()
    true_false = _question(answer_mode=QuizAnswerMode.TRUE_FALSE)

    assert single.correct_options == (single.options[0],)
    assert true_false.answer_mode is QuizAnswerMode.TRUE_FALSE

    invalid = single.model_dump()
    invalid["options"][1]["position"] = 3
    with pytest.raises(ValidationError, match="contiguous"):
        QuizQuestion.model_validate(invalid)
    invalid = single.model_dump()
    invalid["options"][1]["is_correct"] = True
    with pytest.raises(ValidationError, match="one answer"):
        QuizQuestion.model_validate(invalid)


def test_manual_quiz_seed_contains_a_varied_validated_question_bank() -> None:
    payload = _seed_payload()
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    slugs = [str(item["slug"]) for item in payload]

    assert len(payload) == 70
    assert len(slugs) == len(set(slugs))
    assert Counter(str(item["answer_mode"]) for item in payload) == {
        "single_choice": 51,
        "true_false": 19,
    }
    assert set(str(item["category"]) for item in payload) == {
        category.value for category in QuizCategory
    }

    for item in payload:
        raw_options = item["options"]
        assert isinstance(raw_options, list)
        question = QuizQuestion.model_validate(
            {
                **item,
                "id": uuid5(NAMESPACE_URL, f"manual-quiz:{item['slug']}"),
                "guild_id": 1,
                "status": "active",
                "revision": 1,
                "options": tuple(
                    {
                        "id": uuid5(
                            NAMESPACE_URL,
                            f"manual-quiz:{item['slug']}:{position}",
                        ),
                        "position": position,
                        "text": option["text"],
                        "is_correct": option["correct"],
                    }
                    for position, option in enumerate(raw_options)
                ),
                "created_at": now,
                "updated_at": now,
            }
        )
        assert question.source_reference.startswith("Manual LSSD · ")


class _QuestionPool:
    def __init__(self, questions: tuple[QuizQuestion, ...]) -> None:
        self.questions = questions

    async def random_active(
        self,
        guild_id: int,
        *,
        exclude_question_ids: set[UUID] | tuple[UUID, ...] = (),
    ) -> QuizQuestion | None:
        excluded = set(exclude_question_ids)
        return next(
            (
                question
                for question in self.questions
                if question.guild_id == guild_id and question.id not in excluded
            ),
            None,
        )


@pytest.mark.asyncio
async def test_manual_quiz_sessions_prevent_concurrency_and_repeat_only_after_cycle() -> None:
    first = _question(question_id=uuid4())
    second = _question(question_id=uuid4())
    sessions = ManualQuizSessionRegistry(  # type: ignore[arg-type]
        _QuestionPool((first, second)),
        cooldown_seconds=0,
    )

    assert await sessions.reserve(1, 11) == first
    with pytest.raises(ActiveQuizSessionError):
        await sessions.reserve(1, 11)
    await sessions.release(1, 11, first.id)
    assert await sessions.reserve(1, 11) == second
    await sessions.release(1, 11, second.id)
    assert await sessions.reserve(1, 11) == first


@pytest.mark.asyncio
async def test_manual_quiz_sessions_enforce_cooldown_and_cancel_failed_publication() -> None:
    now = [10.0]
    question = _question()
    sessions = ManualQuizSessionRegistry(  # type: ignore[arg-type]
        _QuestionPool((question,)),
        cooldown_seconds=30,
        clock=lambda: now[0],
    )

    reserved = await sessions.reserve(1, 11)
    await sessions.release(1, 11, reserved.id)
    with pytest.raises(QuizCooldownError) as caught:
        await sessions.reserve(1, 11)
    assert caught.value.remaining_seconds == 30

    now[0] = 40.0
    reserved = await sessions.reserve(1, 11)
    await sessions.cancel(1, 11, reserved.id)
    assert not await sessions.is_active(1, 11)
    assert await sessions.reserve(1, 11) == question


@pytest.mark.asyncio
async def test_manual_quiz_command_rejects_non_operational_member_without_querying(persona) -> None:
    responses: list[dict[str, object]] = []
    member = _member(11, (999,))

    async def cached(_guild_id: int):
        return SimpleNamespace(
            settings=SimpleNamespace(operational_staff_role_ids=(200,)),
            persona=SimpleNamespace(profile=persona),
        )

    async def reserve(*_args: object):
        raise AssertionError("unauthorized members must not query the catalogue")

    async def respond(**kwargs: object) -> None:
        responses.append(kwargs)

    cog = ManualQuizCog(
        SimpleNamespace(cached=cached),
        SimpleNamespace(reserve=reserve),
        persona,
    )
    context = SimpleNamespace(
        guild=member.guild,
        author=member,
        channel=SimpleNamespace(id=22),
        respond=respond,
    )

    await ManualQuizCog.manual_question.callback(cog, context)  # type: ignore[arg-type]

    assert responses[0]["ephemeral"] is True
    assert responses[0]["embed"].description == persona.messages.interface.manual_quiz.unauthorized


@pytest.mark.asyncio
async def test_manual_quiz_command_publishes_question_for_operational_member(persona) -> None:
    responses: list[dict[str, object]] = []
    member = _member(11, (200,))
    question = _question()

    async def cached(_guild_id: int):
        return SimpleNamespace(
            settings=SimpleNamespace(operational_staff_role_ids=(200,)),
            persona=SimpleNamespace(profile=persona),
        )

    async def reserve(_guild_id: int, _user_id: int) -> QuizQuestion:
        return question

    async def respond(**kwargs: object) -> None:
        responses.append(kwargs)

    cog = ManualQuizCog(
        SimpleNamespace(cached=cached),
        SimpleNamespace(reserve=reserve),
        persona,
    )
    context = SimpleNamespace(
        guild=member.guild,
        author=member,
        channel=SimpleNamespace(id=22),
        respond=respond,
    )

    await ManualQuizCog.manual_question.callback(cog, context)  # type: ignore[arg-type]

    assert responses[0]["ephemeral"] is False
    assert isinstance(responses[0]["view"], ManualQuizView)
    assert question.prompt in responses[0]["embed"].fields[0].value
    assert "Respuesta abierta" in responses[0]["embed"].footer.text


@pytest.mark.asyncio
async def test_manual_quiz_view_accepts_first_public_answer_and_edits_same_message(persona) -> None:
    question = _question()
    releases: list[tuple[int, int, UUID]] = []

    async def release(guild_id: int, user_id: int, question_id: UUID) -> None:
        releases.append((guild_id, user_id, question_id))

    view = ManualQuizView(
        publisher_id=11,
        guild_id=1,
        question=question,
        ui=persona.messages.interface.manual_quiz,
        sessions=SimpleNamespace(release=release),  # type: ignore[arg-type]
    )
    edits: list[dict[str, object]] = []

    async def edit_message(**kwargs: object) -> None:
        edits.append(kwargs)

    public_interaction = SimpleNamespace(
        user=SimpleNamespace(id=12, display_name="Bob Deputy"),
        response=SimpleNamespace(edit_message=edit_message),
    )
    await view.submit(public_interaction, question.options[1].id)  # type: ignore[arg-type]

    assert edits[0]["embed"].title == "❌ Respuesta incorrecta"
    assert any(
        field.name == "Respuesta de Bob Deputy" for field in edits[0]["embed"].fields
    )
    assert any(field.name == "Explicación" for field in edits[0]["embed"].fields)
    assert all(item.disabled for item in view.children)
    assert releases == [(1, 11, question.id)]

    late_responses: list[dict[str, object]] = []

    async def send_message(**kwargs: object) -> None:
        late_responses.append(kwargs)

    late_interaction = SimpleNamespace(
        user=SimpleNamespace(id=13, display_name="Carol Deputy"),
        response=SimpleNamespace(send_message=send_message),
    )
    await view.submit(late_interaction, question.options[0].id)  # type: ignore[arg-type]

    assert late_responses[0]["ephemeral"] is True
    assert (
        late_responses[0]["embed"].description
        == persona.messages.interface.manual_quiz.already_answered
    )


def test_manual_quiz_embeds_keep_formal_data_and_personality_copy(persona) -> None:
    question = _question()
    ui = persona.messages.interface.manual_quiz

    pending = build_question_embed(question, ui, publisher_name="Alice Deputy")
    result = build_result_embed(
        question,
        question.options[0],
        ui,
        respondent_id=12,
        respondent_name="Bob Deputy",
    )

    assert pending.description == ui.question_intro
    assert "Dificultad Básica" in pending.footer.text
    assert "Respuesta abierta" in pending.footer.text
    assert pending.author.name == "Publicada por Alice Deputy"
    assert result.title == "✅ Respuesta correcta"
    assert result.description in ui.correct_messages
    assert result.fields[1].name == "Respuesta de Bob Deputy"
    assert question.source_reference in result.fields[-1].value
    assert "no registra puntaje" in result.footer.text


@pytest.mark.asyncio
async def test_manual_quiz_repository_reads_active_mvp_question_from_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("requires TEST_DATABASE_URL with an isolated PostgreSQL database")
    monkeypatch.setenv("DATABASE_URL", database_url)
    await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    guild_id = 876543210123456700
    question_id = uuid4()
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    try:
        await client.execute(discord_guilds.delete().where(discord_guilds.c.discord_id == guild_id))
        await client.execute(
            discord_guilds.insert().values(discord_id=guild_id, name="Manual quiz QA")
        )
        await client.execute(
            manual_quiz_questions.insert().values(
                id=question_id,
                guild_id=guild_id,
                slug="integration_question",
                answer_mode="single_choice",
                category="procedure",
                difficulty="basic",
                prompt="Pregunta de integración",
                explanation="Explicación de integración",
                source_reference="Manual · QA",
                status="active",
                revision=1,
                created_at=now,
                updated_at=now,
            )
        )
        await client.execute(
            manual_quiz_options.insert().values(
                [
                    {
                        "id": uuid4(),
                        "question_id": question_id,
                        "position": 0,
                        "text": "Correcta",
                        "is_correct": True,
                    },
                    {
                        "id": uuid4(),
                        "question_id": question_id,
                        "position": 1,
                        "text": "Incorrecta",
                        "is_correct": False,
                    },
                ]
            )
        )

        repository = ManualQuizRepository(client)
        loaded = await repository.random_active(guild_id)

        assert loaded is not None
        assert loaded.id == question_id
        assert loaded.slug == "integration_question"
        assert loaded.correct_options[0].text == "Correcta"
        assert await repository.random_active(
            guild_id,
            exclude_question_ids=(question_id,),
        ) is None
    finally:
        await client.execute(discord_guilds.delete().where(discord_guilds.c.discord_id == guild_id))
        await client.close()


def test_manual_question_command_is_registered_at_root(persona) -> None:
    cog = ManualQuizCog(SimpleNamespace(), SimpleNamespace(), persona)

    assert cog.manual_question.name == "pregunta-manual"
