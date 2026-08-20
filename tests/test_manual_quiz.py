from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import discord
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from pydantic import ValidationError

from arrest_report_agent.infrastructure.postgres import PostgresClient, PostgresClientConfig
from arrest_report_agent.infrastructure.postgres.schema import (
    discord_guilds,
    manual_quiz_options,
    manual_quiz_questions,
    manual_quiz_rotation_entries,
    manual_quiz_rotations,
)
from arrest_report_agent.manual_quiz import (
    ActiveQuizSessionError,
    ManualQuizCog,
    ManualQuizConfigurationModal,
    ManualQuizRepository,
    ManualQuizSessionRegistry,
    ManualQuizView,
    QuizAnswerMode,
    QuizCategory,
    QuizCooldownError,
    QuizDifficulty,
    QuizDurationSeconds,
    QuizOption,
    QuizQuestion,
    QuizQuestionReservation,
    QuizQuestionStatus,
)
from arrest_report_agent.manual_quiz.discord import (
    build_question_embed,
    build_result_embed,
    build_timeout_embed,
)

SEED_SQL_PATH = Path(__file__).parents[1] / "scripts" / "seed_manual_quiz_questions.sql"
ADVANCED_MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260820_0042_manual_quiz_rotation.py"
)


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
    difficulty: QuizDifficulty = QuizDifficulty.BASIC,
) -> QuizQuestion:
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    return QuizQuestion(
        id=question_id or uuid4(),
        guild_id=guild_id,
        slug="qa_question",
        answer_mode=answer_mode,
        category=QuizCategory.PROCEDURE,
        difficulty=difficulty,
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

    assert len(payload) == 120
    assert len(slugs) == len(set(slugs))
    assert Counter(str(item["answer_mode"]) for item in payload) == {
        "single_choice": 101,
        "true_false": 19,
    }
    assert Counter(str(item["difficulty"]) for item in payload) == {
        "basic": 35,
        "intermediate": 29,
        "advanced": 56,
    }
    assert sum(str(item["slug"]).startswith("advanced_") for item in payload) == 50
    assert set(str(item["category"]) for item in payload) == {category.value for category in QuizCategory}

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


def test_manual_quiz_advanced_migration_catalog_matches_seed_backup() -> None:
    spec = importlib.util.spec_from_file_location("manual_quiz_rotation_migration", ADVANCED_MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    migration_payload = list(migration._catalog())
    advanced_seed = [
        item for item in _seed_payload() if str(item["slug"]).startswith("advanced_")
    ]

    assert migration_payload == advanced_seed
    assert len(migration_payload) == 50
    assert all(item["difficulty"] == "advanced" for item in migration_payload)
    assert all(item["answer_mode"] == "single_choice" for item in migration_payload)
    assert all(len(item["options"]) == 4 for item in migration_payload)


class _QuestionPool:
    def __init__(self, questions: tuple[QuizQuestion, ...]) -> None:
        self.questions = questions
        self.seen: dict[tuple[int, str, str], set[UUID]] = {}
        self.cycles: dict[tuple[int, str, str], int] = {}

    async def reserve_random_active(
        self,
        guild_id: int,
        *,
        answer_mode: QuizAnswerMode | None = None,
        difficulty: QuizDifficulty | None = None,
    ) -> QuizQuestionReservation | None:
        answer_mode_filter = answer_mode.value if answer_mode is not None else "any"
        difficulty_filter = difficulty.value if difficulty is not None else "any"
        scope = (guild_id, answer_mode_filter, difficulty_filter)
        candidates = tuple(
            question
            for question in self.questions
            if question.guild_id == guild_id
            and (answer_mode is None or question.answer_mode is answer_mode)
            and (difficulty is None or question.difficulty is difficulty)
        )
        if not candidates:
            return None
        seen = self.seen.setdefault(scope, set())
        question = next(
            (
                question
                for question in candidates
                if question.id not in seen
            ),
            None,
        )
        if question is None:
            seen.clear()
            self.cycles[scope] = self.cycles.get(scope, 1) + 1
            question = candidates[0]
        cycle = self.cycles.setdefault(scope, 1)
        seen.add(question.id)
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
        scope = (
            guild_id,
            reservation.answer_mode_filter,
            reservation.difficulty_filter,
        )
        if self.cycles.get(scope, 1) == reservation.cycle:
            self.seen.get(scope, set()).discard(reservation.question.id)


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
async def test_manual_quiz_rotation_is_shared_by_users_and_survives_registry_restart() -> None:
    first = _question(question_id=uuid4())
    second = _question(question_id=uuid4())
    repository = _QuestionPool((first, second))
    initial = ManualQuizSessionRegistry(repository, cooldown_seconds=0)  # type: ignore[arg-type]

    assert await initial.reserve(1, 11) == first
    await initial.release(1, 11, first.id)
    assert await initial.reserve(1, 22) == second
    await initial.release(1, 22, second.id)

    restarted = ManualQuizSessionRegistry(repository, cooldown_seconds=0)  # type: ignore[arg-type]
    assert await restarted.reserve(1, 33) == first


@pytest.mark.asyncio
async def test_manual_quiz_sessions_apply_type_and_difficulty_filters() -> None:
    basic = _question(question_id=uuid4())
    intermediate_true_false = _question(
        question_id=uuid4(),
        answer_mode=QuizAnswerMode.TRUE_FALSE,
        difficulty=QuizDifficulty.INTERMEDIATE,
    )
    sessions = ManualQuizSessionRegistry(  # type: ignore[arg-type]
        _QuestionPool((basic, intermediate_true_false)),
        cooldown_seconds=0,
    )

    selected = await sessions.reserve(
        1,
        11,
        answer_mode=QuizAnswerMode.TRUE_FALSE,
        difficulty=QuizDifficulty.INTERMEDIATE,
    )

    assert selected == intermediate_true_false


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
async def test_manual_quiz_command_opens_configuration_modal_for_operational_member(persona) -> None:
    modals: list[discord.ui.Modal] = []
    member = _member(11, (200,))

    async def cached(_guild_id: int):
        return SimpleNamespace(
            settings=SimpleNamespace(operational_staff_role_ids=(200,)),
            persona=SimpleNamespace(profile=persona),
        )

    async def send_modal(modal: discord.ui.Modal) -> None:
        modals.append(modal)

    cog = ManualQuizCog(
        SimpleNamespace(cached=cached),
        SimpleNamespace(),
        persona,
    )
    context = SimpleNamespace(
        guild=member.guild,
        author=member,
        channel=SimpleNamespace(id=22),
        send_modal=send_modal,
    )

    await ManualQuizCog.manual_question.callback(cog, context)  # type: ignore[arg-type]

    assert len(modals) == 1
    assert isinstance(modals[0], ManualQuizConfigurationModal)


@pytest.mark.asyncio
async def test_manual_quiz_configuration_modal_passes_selected_filters(persona) -> None:
    member = _member(11, (200,))
    calls: list[dict[str, object]] = []

    async def publish_question(
        interaction: object,
        selected_member: discord.Member,
        **kwargs: object,
    ) -> None:
        calls.append({"interaction": interaction, "member": selected_member, **kwargs})

    cog = SimpleNamespace(
        publish_question=publish_question,
        default_persona=persona,
    )
    modal = ManualQuizConfigurationModal(cog=cog, requester_id=member.id)  # type: ignore[arg-type]
    modal.difficulty._interaction = SimpleNamespace(data={})  # noqa: SLF001
    modal.difficulty._selected_values = [QuizDifficulty.ADVANCED.value]  # noqa: SLF001
    modal.answer_mode._interaction = SimpleNamespace(data={})  # noqa: SLF001
    modal.answer_mode._selected_values = [QuizAnswerMode.TRUE_FALSE.value]  # noqa: SLF001
    modal.duration._interaction = SimpleNamespace(data={})  # noqa: SLF001
    modal.duration._selected_values = [str(QuizDurationSeconds.FIFTEEN.value)]  # noqa: SLF001
    interaction = SimpleNamespace(user=member)

    await modal.callback(interaction)  # type: ignore[arg-type]

    assert calls[0]["difficulty"] is QuizDifficulty.ADVANCED
    assert calls[0]["answer_mode"] is QuizAnswerMode.TRUE_FALSE
    assert calls[0]["duration"] is QuizDurationSeconds.FIFTEEN


@pytest.mark.asyncio
async def test_manual_quiz_publish_uses_selected_filters_and_duration(persona) -> None:
    member = _member(11, (200,))
    question = _question(
        answer_mode=QuizAnswerMode.TRUE_FALSE,
        difficulty=QuizDifficulty.INTERMEDIATE,
    )
    reservations: list[dict[str, object]] = []
    sent: list[dict[str, object]] = []

    async def cached(_guild_id: int):
        return SimpleNamespace(
            settings=SimpleNamespace(operational_staff_role_ids=(200,)),
            persona=SimpleNamespace(profile=persona),
        )

    async def reserve(guild_id: int, user_id: int, **kwargs: object) -> QuizQuestion:
        reservations.append({"guild_id": guild_id, "user_id": user_id, **kwargs})
        return question

    async def send_message(**kwargs: object) -> None:
        sent.append(kwargs)

    async def original_response() -> object:
        return SimpleNamespace(edit=lambda **_kwargs: None)

    interaction = SimpleNamespace(
        guild=member.guild,
        channel=SimpleNamespace(id=22),
        response=SimpleNamespace(send_message=send_message, is_done=lambda: False),
        original_response=original_response,
    )
    cog = ManualQuizCog(
        SimpleNamespace(cached=cached),
        SimpleNamespace(reserve=reserve),
        persona,
    )

    await cog.publish_question(
        interaction,  # type: ignore[arg-type]
        member,
        answer_mode=QuizAnswerMode.TRUE_FALSE,
        difficulty=QuizDifficulty.INTERMEDIATE,
        duration=QuizDurationSeconds.FIFTEEN,
    )

    assert reservations == [
        {
            "guild_id": 1,
            "user_id": 11,
            "answer_mode": QuizAnswerMode.TRUE_FALSE,
            "difficulty": QuizDifficulty.INTERMEDIATE,
        }
    ]
    assert sent[0]["ephemeral"] is False
    assert isinstance(sent[0]["view"], ManualQuizView)
    assert sent[0]["view"].duration_seconds is QuizDurationSeconds.FIFTEEN
    assert "15 segundos" in sent[0]["embed"].footer.text


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
        duration_seconds=QuizDurationSeconds.THREE_MINUTES,
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
        field.name == "Respondió" and field.value == "<@12>"
        for field in edits[0]["embed"].fields
    )
    assert any(
        field.name == "Respuesta seleccionada" and field.value == question.options[1].text
        for field in edits[0]["embed"].fields
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
    assert late_responses[0]["embed"].description == persona.messages.interface.manual_quiz.already_answered


@pytest.mark.asyncio
async def test_manual_quiz_timeout_edits_same_message_and_releases_session(persona) -> None:
    question = _question()
    releases: list[tuple[int, int, UUID]] = []
    edits: list[dict[str, object]] = []

    async def release(guild_id: int, user_id: int, question_id: UUID) -> None:
        releases.append((guild_id, user_id, question_id))

    async def edit(**kwargs: object) -> None:
        edits.append(kwargs)

    view = ManualQuizView(
        publisher_id=11,
        guild_id=1,
        question=question,
        ui=persona.messages.interface.manual_quiz,
        sessions=SimpleNamespace(release=release),  # type: ignore[arg-type]
        duration_seconds=QuizDurationSeconds.TEN,
    )
    view.message = SimpleNamespace(edit=edit)

    await view.on_timeout()

    assert edits[0]["embed"].title == "⌛ Tiempo agotado"
    assert any(field.name == "Respuesta correcta" for field in edits[0]["embed"].fields)
    assert all(item.disabled for item in view.children)
    assert releases == [(1, 11, question.id)]


@pytest.mark.asyncio
async def test_manual_quiz_rejects_answer_after_fixed_deadline(persona) -> None:
    question = _question()
    releases: list[tuple[int, int, UUID]] = []
    edits: list[dict[str, object]] = []

    async def release(guild_id: int, user_id: int, question_id: UUID) -> None:
        releases.append((guild_id, user_id, question_id))

    async def edit_message(**kwargs: object) -> None:
        edits.append(kwargs)

    view = ManualQuizView(
        publisher_id=11,
        guild_id=1,
        question=question,
        ui=persona.messages.interface.manual_quiz,
        sessions=SimpleNamespace(release=release),  # type: ignore[arg-type]
        duration_seconds=QuizDurationSeconds.TEN,
    )
    view.expires_at = 0
    interaction = SimpleNamespace(
        response=SimpleNamespace(edit_message=edit_message),
        user=SimpleNamespace(id=12, display_name="Bob Deputy"),
    )

    await view.submit(interaction, question.options[0].id)  # type: ignore[arg-type]

    assert edits[0]["embed"].title == "⌛ Tiempo agotado"
    assert releases == [(1, 11, question.id)]


def test_manual_quiz_embeds_keep_formal_data_and_personality_copy(persona) -> None:
    question = _question()
    ui = persona.messages.interface.manual_quiz

    pending = build_question_embed(
        question,
        ui,
        publisher_name="Alice Deputy",
        duration_seconds=QuizDurationSeconds.FIFTEEN,
    )
    result = build_result_embed(
        question,
        question.options[0],
        ui,
        respondent_id=12,
    )
    timed_out = build_timeout_embed(
        question,
        ui,
        duration_seconds=QuizDurationSeconds.FIFTEEN,
        publisher_id=11,
    )

    assert pending.description == ui.question_intro
    assert "Dificultad Básica" in pending.footer.text
    assert "Respuesta abierta durante 15 segundos" in pending.footer.text
    assert pending.author.name == "Publicada por Alice Deputy"
    assert result.title == "✅ Respuesta correcta"
    assert result.description in ui.correct_messages
    assert result.fields[1].name == "Respondió"
    assert result.fields[1].value == "<@12>"
    assert result.fields[2].name == "Respuesta seleccionada"
    assert question.source_reference in result.fields[-1].value
    assert "no registra puntaje" in result.footer.text
    assert timed_out.description in ui.expired_messages
    assert "15 segundos" in timed_out.footer.text


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
    second_question_id = uuid4()
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    try:
        await client.execute(discord_guilds.delete().where(discord_guilds.c.discord_id == guild_id))
        await client.execute(discord_guilds.insert().values(discord_id=guild_id, name="Manual quiz QA"))
        await client.execute(
            manual_quiz_questions.insert().values(
                [
                    {
                        "id": question_id,
                        "guild_id": guild_id,
                        "slug": "integration_question",
                        "answer_mode": "single_choice",
                        "category": "procedure",
                        "difficulty": "basic",
                        "prompt": "Pregunta de integración",
                        "explanation": "Explicación de integración",
                        "source_reference": "Manual · QA",
                        "status": "active",
                        "revision": 1,
                        "created_at": now,
                        "updated_at": now,
                    },
                    {
                        "id": second_question_id,
                        "guild_id": guild_id,
                        "slug": "integration_question_two",
                        "answer_mode": "single_choice",
                        "category": "procedure",
                        "difficulty": "basic",
                        "prompt": "Segunda pregunta de integración",
                        "explanation": "Segunda explicación de integración",
                        "source_reference": "Manual · QA",
                        "status": "active",
                        "revision": 1,
                        "created_at": now,
                        "updated_at": now,
                    },
                ]
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
                    {
                        "id": uuid4(),
                        "question_id": second_question_id,
                        "position": 0,
                        "text": "Segunda correcta",
                        "is_correct": True,
                    },
                    {
                        "id": uuid4(),
                        "question_id": second_question_id,
                        "position": 1,
                        "text": "Segunda incorrecta",
                        "is_correct": False,
                    },
                ]
            )
        )

        repository = ManualQuizRepository(client)
        loaded = await repository.random_active(guild_id)

        assert loaded is not None
        assert loaded.id in {question_id, second_question_id}
        assert loaded.slug in {"integration_question", "integration_question_two"}
        assert loaded.correct_options[0].text in {"Correcta", "Segunda correcta"}
        assert (
            await repository.random_active(
                guild_id,
                exclude_question_ids=(question_id, second_question_id),
            )
            is None
        )

        first_reservation, second_reservation = await asyncio.gather(
            repository.reserve_random_active(guild_id),
            repository.reserve_random_active(guild_id),
        )
        assert first_reservation is not None
        assert second_reservation is not None
        assert first_reservation.question.id != second_reservation.question.id

        restarted_repository = ManualQuizRepository(client)
        next_cycle = await restarted_repository.reserve_random_active(guild_id)
        assert next_cycle is not None
        assert next_cycle.cycle == 2
        await restarted_repository.cancel_reservation(guild_id, next_cycle)
        assert not await client.fetch_all(
            sa.select(manual_quiz_rotation_entries).where(
                manual_quiz_rotation_entries.c.guild_id == guild_id,
                manual_quiz_rotation_entries.c.cycle == 2,
            )
        )
        rotation = await client.fetch_one(
            sa.select(manual_quiz_rotations).where(
                manual_quiz_rotations.c.guild_id == guild_id,
            )
        )
        assert rotation is not None
        assert rotation["cycle"] == 2
    finally:
        await client.execute(discord_guilds.delete().where(discord_guilds.c.discord_id == guild_id))
        await client.close()


def test_manual_question_command_is_registered_at_root(persona) -> None:
    cog = ManualQuizCog(SimpleNamespace(), SimpleNamespace(), persona)

    assert cog.manual_question.name == "pregunta-manual"
