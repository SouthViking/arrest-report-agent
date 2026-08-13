from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config

from arrest_report_agent.infrastructure.postgres import PostgresClient, PostgresClientConfig
from arrest_report_agent.infrastructure.postgres.schema import discord_guilds
from arrest_report_agent.suggestions import (
    MAX_PENDING_SUGGESTIONS,
    SuggestionCategory,
    SuggestionCog,
    SuggestionCooldownError,
    SuggestionEventType,
    SuggestionLimitReachedError,
    SuggestionRepository,
    SuggestionStatus,
)
from arrest_report_agent.suggestions.discord import (
    CATEGORY_PRESENTATION,
    SuggestionCreateModal,
    SuggestionStatusModal,
    build_detail_embed,
    build_inbox_embed,
    build_info_embed,
)
from arrest_report_agent.suggestions.models import Suggestion


def test_suggestion_commands_are_root_commands() -> None:
    commands = (
        SuggestionCog.create_suggestion,
        SuggestionCog.info_suggestions,
        SuggestionCog.suggestion_inbox,
        SuggestionCog.change_suggestion_status,
    )

    assert [command.name for command in commands] == [
        "sugerencia",
        "info-sugerencias",
        "sugerencias",
        "estado-sugerencia",
    ]
    assert all(command.parent is None for command in commands)


@pytest.mark.asyncio
async def test_suggestion_modals_are_argument_free_and_visual(persona) -> None:
    ui = persona.messages.interface.suggestions
    cog = SimpleNamespace()
    create = SuggestionCreateModal(
        cog,
        requester_id=11,
        guild_id=1,
        source_channel_id=2,
        ui=ui,
    )
    status = SuggestionStatusModal(cog, requester_id=11, guild_id=1, ui=ui)

    assert len(create.category.options) == 6
    assert {option.value for option in create.category.options} == {
        category.value for category in SuggestionCategory
    }
    assert all(option.emoji is not None for option in create.category.options)
    assert create.description_input.max_length == 1500
    assert [option.value for option in status.status.options] == ["pending", "archived"]


def test_public_suggestion_info_is_concise_and_hides_owner_commands(persona) -> None:
    embed = build_info_embed(persona.messages.interface.suggestions)
    rendered = "\n".join(
        [embed.title or "", embed.description or ""]
        + [f"{field.name}\n{field.value}" for field in embed.fields]
    )

    assert "/sugerencia" in rendered
    assert "/sugerencias" not in rendered
    assert "/estado-sugerencia" not in rendered
    assert str(MAX_PENDING_SUGGESTIONS) in rendered
    assert "sancionados" in rendered
    assert all(presentation.emoji in rendered for presentation in CATEGORY_PRESENTATION.values())


def test_suggestion_embeds_show_reference_status_and_private_detail(persona) -> None:
    ui = persona.messages.interface.suggestions
    now = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    suggestion = Suggestion(
        id="00000000-0000-0000-0000-000000000001",
        guild_id=1,
        number=12,
        author_user_id=20,
        source_channel_id=30,
        category=SuggestionCategory.AUTOMATION,
        title="Mejorar el pager",
        description="Permitir un filtro adicional en el listado.",
        status=SuggestionStatus.PENDING,
        version=1,
        created_at=now,
        updated_at=now,
    )

    inbox = build_inbox_embed((suggestion,), total=1, page=0, ui=ui)
    detail = build_detail_embed(suggestion, ui)

    assert suggestion.reference == "SUG-00012"
    assert "SUG-00012" in inbox.fields[0].name
    assert "<@20>" in inbox.fields[0].value
    assert detail.description == suggestion.description
    assert any(field.name == "Origen" and "<#30>" in field.value for field in detail.fields)


@pytest.mark.asyncio
async def test_suggestion_repository_enforces_cooldown_quota_and_status_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("requires TEST_DATABASE_URL with an isolated PostgreSQL database")
    monkeypatch.setenv("DATABASE_URL", database_url)
    await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    guild_id = 876543210123456789
    second_guild_id = 876543210123456788
    concurrent_guild_id = 876543210123456787
    start = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    try:
        await client.execute(
            discord_guilds.delete().where(
                discord_guilds.c.discord_id.in_((guild_id, second_guild_id, concurrent_guild_id))
            )
        )
        await client.execute(
            discord_guilds.insert().values(
                [
                    {"discord_id": guild_id, "name": "Suggestion QA"},
                    {"discord_id": second_guild_id, "name": "Suggestion isolation QA"},
                    {"discord_id": concurrent_guild_id, "name": "Suggestion concurrency QA"},
                ]
            )
        )
        repository = SuggestionRepository(client)
        first = await repository.submit(
            guild_id=guild_id,
            author_user_id=11,
            source_channel_id=100,
            category=SuggestionCategory.AUTOMATION,
            title="Primera idea",
            description="Una descripción suficientemente clara.",
            now=start,
        )
        with pytest.raises(SuggestionCooldownError):
            await repository.submit(
                guild_id=guild_id,
                author_user_id=11,
                source_channel_id=100,
                category=SuggestionCategory.OTHER,
                title="Demasiado rápida",
                description="Este envío debe quedar bloqueado por cooldown.",
                now=start + timedelta(seconds=1),
            )
        for position in (1, 2):
            await repository.submit(
                guild_id=guild_id,
                author_user_id=11,
                source_channel_id=100,
                category=SuggestionCategory.PROCEDURE,
                title=f"Idea número {position + 1}",
                description="Otra descripción válida para completar la cuota.",
                now=start + timedelta(seconds=61 * position),
            )
        with pytest.raises(SuggestionLimitReachedError):
            await repository.submit(
                guild_id=guild_id,
                author_user_id=11,
                source_channel_id=100,
                category=SuggestionCategory.OTHER,
                title="Cuarta idea pendiente",
                description="La cuota debe bloquear esta sugerencia.",
                now=start + timedelta(seconds=183),
            )
        archived, changed = await repository.change_status(
            guild_id=guild_id,
            number=first.number,
            status=SuggestionStatus.ARCHIVED,
            actor_user_id=99,
            now=start + timedelta(seconds=184),
        )
        assert changed is True
        assert archived.status is SuggestionStatus.ARCHIVED
        fourth = await repository.submit(
            guild_id=guild_id,
            author_user_id=11,
            source_channel_id=100,
            category=SuggestionCategory.CORRECTION,
            title="Cupo nuevamente disponible",
            description="Archivar una sugerencia debe liberar un cupo pendiente.",
            now=start + timedelta(seconds=244),
        )
        items, total = await repository.list_for_guild(guild_id, offset=0, limit=5)
        events = await repository.list_events(first.id)

        assert total == 4
        assert items[0] == fourth
        assert [event.event_type for event in events] == [
            SuggestionEventType.SUBMITTED,
            SuggestionEventType.STATUS_CHANGED,
        ]
        assert events[-1].actor_user_id == 99
        assert await repository.get_by_number(second_guild_id, first.number) is None

        concurrent = await asyncio.gather(
            *(
                repository.submit(
                    guild_id=concurrent_guild_id,
                    author_user_id=500 + position,
                    source_channel_id=100,
                    category=SuggestionCategory.OTHER,
                    title=f"Idea concurrente {position}",
                    description="Valida que el correlativo sea atómico entre usuarios.",
                    now=start,
                )
                for position in range(8)
            )
        )
        assert sorted(item.number for item in concurrent) == list(range(1, 9))

        simultaneous = await asyncio.gather(
            *(
                repository.submit(
                    guild_id=second_guild_id,
                    author_user_id=700,
                    source_channel_id=100,
                    category=SuggestionCategory.OTHER,
                    title=f"Intento simultáneo {position}",
                    description="Solo un envío simultáneo del mismo usuario puede registrarse.",
                    now=start,
                )
                for position in range(5)
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(result, Suggestion) for result in simultaneous) == 1
        assert sum(isinstance(result, SuggestionCooldownError) for result in simultaneous) == 4
    finally:
        await client.close()
