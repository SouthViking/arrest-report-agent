from __future__ import annotations

from types import SimpleNamespace

import discord
import pytest

from arrest_report_agent.infrastructure.discord.cog import (
    PENAL_CODE_URL,
    PROCEDURES_MANUAL_URL,
    UNAUTHORIZED_USER_INTROS,
    WRONG_CHANNEL_INTROS,
    ReviewCog,
    build_help_embed,
    build_unauthorized_user_message,
    build_wrong_channel_message,
    maintain_typing_indicator,
)


class FakeTypingContext:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.active = False

    async def __aenter__(self) -> None:
        if self.error:
            raise self.error
        self.active = True

    async def __aexit__(self, *_args) -> None:
        self.active = False


class FakeTypingChannel:
    id = 123

    def __init__(self, context: FakeTypingContext) -> None:
        self.context = context

    def typing(self) -> FakeTypingContext:
        return self.context


class UnexpectedReviewService:
    async def review(self, *_args, **_kwargs) -> None:
        raise AssertionError("The agent runner must not execute for a wrong-channel redirect")


class UnexpectedContextBuilder:
    async def resolve_target(self, *_args, **_kwargs):
        raise AssertionError("Discord context must not be built for a blocked user")


def test_operational_command_group_uses_distinct_wv_namespace() -> None:
    assert ReviewCog.wv.name == "wv"
    assert [command.name for command in ReviewCog.wv.subcommands] == ["help"]


def test_help_embed_uses_runtime_identity_channels_and_bot_mention() -> None:
    embed = build_help_embed(
        display_name="Sgt. Rivera",
        role_title="Watch Sergeant",
        bot_mention="<@999>",
        channel_ids=frozenset({222, 111}),
        avatar_url="https://cdn.discordapp.com/avatar.png",
    )

    assert embed.title == "⭐ Sgt. Rivera · Watch Sergeant"
    assert "Sgt. Rivera" in embed.description
    assert [field.name for field in embed.fields] == [
        "📋 Qué reviso",
        "📍 Dónde estoy de guardia",
        "📣 Cómo solicitar una revisión",
        "🧵 Qué haré con el expediente",
        "📚 Documentación oficial",
    ]
    assert embed.fields[1].value == "• <#111>\n• <#222>"
    assert "<@999>" in embed.fields[2].value
    assert embed.fields[4].value == (
        f"• [Código Penal]({PENAL_CODE_URL})\n• [Manual de Procedimientos]({PROCEDURES_MANUAL_URL})"
    )
    assert embed.thumbnail.url == "https://cdn.discordapp.com/avatar.png"
    assert "Walter Vigil" not in str(embed.to_dict())


def test_help_embed_describes_unrestricted_channel_scope() -> None:
    embed = build_help_embed(
        display_name="Sgt. Rivera",
        role_title="Watch Sergeant",
        bot_mention="<@999>",
        channel_ids=None,
    )

    assert embed.fields[1].value == "• Todos los canales del servidor"


def test_wrong_channel_message_varies_copy_but_keeps_stable_format() -> None:
    messages = [
        build_wrong_channel_message(
            channel_ids=frozenset({222, 111}),
            message_id=message_id,
        )
        for message_id in range(len(WRONG_CHANNEL_INTROS))
    ]

    for intro, message in zip(WRONG_CHANNEL_INTROS, messages, strict=True):
        assert message == (
            f"📍 **Fuera de mi zona de guardia, deputy.** {intro}\n"
            "Puede encontrarme en <#111> o <#222>; mencióneme allí para continuar."
        )


def test_unauthorized_user_message_varies_copy_but_keeps_stable_format() -> None:
    messages = [
        build_unauthorized_user_message(message_id=message_id)
        for message_id in range(len(UNAUTHORIZED_USER_INTROS))
    ]

    for intro, message in zip(UNAUTHORIZED_USER_INTROS, messages, strict=True):
        assert message == (
            f"🔒 **Un momento, deputy.** {intro}\n"
            "Consulte con un mando para que revise su habilitación antes de continuar."
        )


@pytest.mark.asyncio
async def test_wrong_channel_mention_replies_once_without_running_agent(settings) -> None:
    replies: list[dict[str, object]] = []

    async def reply(**kwargs) -> None:
        replies.append(kwargs)

    bot = SimpleNamespace(
        user=SimpleNamespace(
            id=999,
            display_avatar=SimpleNamespace(url="https://cdn.discordapp.com/bot.png"),
        )
    )
    cog = ReviewCog(bot, settings, UnexpectedReviewService())  # type: ignore[arg-type]
    message = SimpleNamespace(
        id=42,
        guild=SimpleNamespace(id=min(settings.discord_guild_ids or ())),
        author=SimpleNamespace(id=123, bot=False),
        webhook_id=None,
        channel=SimpleNamespace(id=555),
        raw_mentions=[999],
        reply=reply,
    )

    await cog.on_message(message)  # type: ignore[arg-type]
    await cog.on_message(message)  # type: ignore[arg-type]

    assert len(replies) == 1
    assert replies[0]["mention_author"] is False
    assert isinstance(replies[0]["allowed_mentions"], discord.AllowedMentions)
    content = replies[0]["content"]
    assert isinstance(content, str)
    assert content.startswith("📍 **Fuera de mi zona de guardia, deputy.**")
    for channel_id in settings.review_channel_ids or ():
        assert f"<#{channel_id}>" in content


@pytest.mark.asyncio
async def test_unauthorized_mention_replies_once_without_context_or_agent(settings) -> None:
    replies: list[dict[str, object]] = []

    async def reply(**kwargs) -> None:
        replies.append(kwargs)

    restricted = settings.model_copy(update={"authorized_user_ids_csv": "321"})
    bot = SimpleNamespace(user=SimpleNamespace(id=999))
    cog = ReviewCog(bot, restricted, UnexpectedReviewService())  # type: ignore[arg-type]
    cog.context_builder = UnexpectedContextBuilder()  # type: ignore[assignment]
    message = SimpleNamespace(
        id=42,
        guild=SimpleNamespace(id=min(restricted.discord_guild_ids or ())),
        author=SimpleNamespace(id=123, bot=False),
        webhook_id=None,
        channel=SimpleNamespace(id=min(restricted.review_channel_ids or ())),
        raw_mentions=[999],
        reply=reply,
    )

    await cog.on_message(message)  # type: ignore[arg-type]
    await cog.on_message(message)  # type: ignore[arg-type]

    assert len(replies) == 1
    assert replies[0]["mention_author"] is False
    assert isinstance(replies[0]["allowed_mentions"], discord.AllowedMentions)
    assert replies[0]["content"] == build_unauthorized_user_message(message_id=42)


@pytest.mark.asyncio
async def test_typing_indicator_stays_active_for_the_operation() -> None:
    context = FakeTypingContext()
    channel = FakeTypingChannel(context)

    async with maintain_typing_indicator(channel):  # type: ignore[arg-type]
        assert context.active is True

    assert context.active is False


@pytest.mark.asyncio
async def test_typing_indicator_failure_does_not_block_the_operation() -> None:
    response = SimpleNamespace(status=500, reason="Internal Server Error")
    error = discord.HTTPException(response, "typing unavailable")
    context = FakeTypingContext(error)
    channel = FakeTypingChannel(context)
    reached_operation = False

    async with maintain_typing_indicator(channel):  # type: ignore[arg-type]
        reached_operation = True

    assert reached_operation is True
