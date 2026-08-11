from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import discord
import pytest

from arrest_report_agent.config import (
    AccessPolicyKey,
    AccessPolicyMode,
    AccessPolicySnapshot,
)
from arrest_report_agent.infrastructure.discord.config_admin import (
    ConfigurationInlineController,
    build_config_unauthorized_embed,
    parse_inline_config_command,
)


class FakeMessage:
    def __init__(
        self,
        content: str,
        *,
        message_id: int,
        user_id: int,
        guild_id: int = 100,
    ) -> None:
        self.content = content
        self.id = message_id
        self.author = SimpleNamespace(id=user_id, bot=False)
        self.guild = SimpleNamespace(id=guild_id)
        self.channel = SimpleNamespace(id=200)
        self.attachments: list[object] = []
        self.replies: list[dict[str, object]] = []

    async def reply(self, **kwargs: object) -> None:
        self.replies.append(kwargs)


class FakeRepository:
    def __init__(self) -> None:
        self.created: list[dict[str, int]] = []

    async def draft_revision_id(self, _guild_id: int):
        return None

    async def create_draft(
        self,
        guild_id: int,
        *,
        actor_user_id: int,
        interaction_id: int,
    ) -> UUID:
        self.created.append(
            {
                "guild_id": guild_id,
                "actor_user_id": actor_user_id,
                "interaction_id": interaction_id,
            }
        )
        return UUID("00000000-0000-0000-0000-000000000123")


class FakeProvider:
    def __init__(self) -> None:
        policy = AccessPolicySnapshot(
            key=AccessPolicyKey.REPORT_CHANNELS,
            subject_type="channel",
            mode=AccessPolicyMode.RESTRICTED,
            ids=(300,),
        )
        self.snapshot = SimpleNamespace(
            revision=4,
            persona_id="roy_velazco",
            persona_revision=24,
            agent=SimpleNamespace(model="gpt-5.6-terra", reasoning_effort="medium"),
            policies={AccessPolicyKey.REPORT_CHANNELS: policy},
        )

    async def cached(self, _guild_id: int):
        return SimpleNamespace(snapshot=self.snapshot)


def test_inline_config_parser_requires_exact_prefix_at_the_start() -> None:
    assert parse_inline_config_command("$rac") == ("ayuda",)
    assert parse_inline_config_command("  $RAC ver") == ("ver",)
    assert parse_inline_config_command("$rac agente reasoning=low") == (
        "agente",
        "reasoning=low",
    )
    assert parse_inline_config_command("hola $rac ver") is None
    assert parse_inline_config_command("$racing ver") is None


def test_unauthorized_embed_uses_the_persona_message_pool(persona) -> None:
    first = build_config_unauthorized_embed(persona, 0)
    second = build_config_unauthorized_embed(persona, 1)

    assert first.title == "🔒 Configuración restringida"
    assert persona.messages.access.unauthorized_user_intros[0] in first.description
    assert persona.messages.access.unauthorized_user_intros[1] in second.description
    assert first.description != second.description


@pytest.mark.asyncio
async def test_unauthorized_inline_command_replies_publicly_and_is_consumed(
    settings,
    persona,
) -> None:
    controller = ConfigurationInlineController(
        settings,
        FakeRepository(),  # type: ignore[arg-type]
        FakeProvider(),  # type: ignore[arg-type]
    )
    message = FakeMessage("$rac ver", message_id=10, user_id=999)

    consumed = await controller.handle(message, persona)  # type: ignore[arg-type]

    assert consumed is True
    assert len(message.replies) == 1
    reply = message.replies[0]
    assert reply["mention_author"] is False
    assert isinstance(reply["allowed_mentions"], discord.AllowedMentions)
    assert isinstance(reply["embed"], discord.Embed)
    assert reply["embed"].title == "🔒 Configuración restringida"


@pytest.mark.asyncio
async def test_unauthorized_inline_command_has_a_per_user_cooldown(settings, persona) -> None:
    controller = ConfigurationInlineController(
        settings,
        FakeRepository(),  # type: ignore[arg-type]
        FakeProvider(),  # type: ignore[arg-type]
    )
    first = FakeMessage("$rac ver", message_id=10, user_id=999)
    second = FakeMessage("$rac ver", message_id=11, user_id=999)

    await controller.handle(first, persona)  # type: ignore[arg-type]
    await controller.handle(second, persona)  # type: ignore[arg-type]

    assert len(first.replies) == 1
    assert second.replies == []


@pytest.mark.asyncio
async def test_owner_can_read_configuration_with_inline_command(settings, persona) -> None:
    owner_settings = settings.model_copy(update={"config_owner_user_ids_csv": "42"})
    controller = ConfigurationInlineController(
        owner_settings,
        FakeRepository(),  # type: ignore[arg-type]
        FakeProvider(),  # type: ignore[arg-type]
    )
    message = FakeMessage("$rac ver", message_id=50, user_id=42)

    consumed = await controller.handle(message, persona)  # type: ignore[arg-type]

    assert consumed is True
    assert len(message.replies) == 1
    embed = message.replies[0]["embed"]
    assert isinstance(embed, discord.Embed)
    assert embed.title == "⚙️ Configuración activa"
    assert "roy_velazco" in embed.description
    assert "report_channels" in embed.description


@pytest.mark.asyncio
async def test_owner_mutation_uses_message_id_as_idempotency_key(settings, persona) -> None:
    owner_settings = settings.model_copy(update={"config_owner_user_ids_csv": "42"})
    repository = FakeRepository()
    controller = ConfigurationInlineController(
        owner_settings,
        repository,  # type: ignore[arg-type]
        FakeProvider(),  # type: ignore[arg-type]
    )
    message = FakeMessage("$rac borrador crear", message_id=51, user_id=42)

    await controller.handle(message, persona)  # type: ignore[arg-type]

    assert repository.created == [{"guild_id": 100, "actor_user_id": 42, "interaction_id": 51}]
    assert message.replies[0]["embed"].title == "📝 Borrador creado"
