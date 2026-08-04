from __future__ import annotations

from types import SimpleNamespace

import discord
import pytest

from arrest_report_agent.infrastructure.discord.cog import (
    PENAL_CODE_URL,
    PROCEDURES_MANUAL_URL,
    UNAUTHORIZED_USER_INTROS,
    WRONG_CHANNEL_INTROS,
    ReviewChannelAvailability,
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


class FakePermissions:
    def __init__(self, *, view_channel: bool) -> None:
        self.view_channel = view_channel


class FakeReviewChannel:
    def __init__(self, channel_id: int, *, hidden_user_ids: set[int] | None = None) -> None:
        self.id = channel_id
        self.hidden_user_ids = hidden_user_ids or set()

    def permissions_for(self, member) -> FakePermissions:
        return FakePermissions(view_channel=member.id not in self.hidden_user_ids)


class FakeGuild:
    def __init__(
        self,
        guild_id: int,
        *,
        bot_member,
        channels: list[FakeReviewChannel] | None = None,
        missing: bool = False,
    ) -> None:
        self.id = guild_id
        self.me = bot_member
        self.channels = {channel.id: channel for channel in channels or []}
        self.missing = missing
        self.fetches: list[int] = []

    def get_channel(self, channel_id: int):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id: int):
        self.fetches.append(channel_id)
        if self.missing:
            response = SimpleNamespace(status=404, reason="Not Found")
            raise discord.NotFound(response, "Unknown Channel")
        return self.channels.get(channel_id)


def test_operational_command_group_uses_distinct_wv_namespace() -> None:
    assert ReviewCog.wv.name == "wv"
    assert [command.name for command in ReviewCog.wv.subcommands] == [
        "help",
        "mis-reportes",
        "mi-resumen",
    ]


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
        "🗂️ Consultas privadas",
        "📚 Documentación oficial",
    ]
    assert embed.fields[1].value == "• <#111>\n• <#222>"
    assert "<@999>" in embed.fields[2].value
    assert "respondes directamente" in embed.fields[2].value
    assert "/wv mis-reportes" in embed.fields[4].value
    assert "/wv mi-resumen" in embed.fields[4].value
    assert embed.fields[5].value == (
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
            channel_ids=(222, 111),
            message_id=message_id,
        )
        for message_id in range(len(WRONG_CHANNEL_INTROS))
    ]

    for intro, message in zip(WRONG_CHANNEL_INTROS, messages, strict=True):
        assert message == (
            f"📍 **Fuera de mi zona de guardia, deputy.** {intro}\n"
            "Puede encontrarme en <#111> o <#222>; mencióneme allí para continuar."
        )


@pytest.mark.parametrize(
    ("availability", "expected"),
    [
        (ReviewChannelAvailability.HIDDEN, "No tiene acceso visible"),
        (ReviewChannelAvailability.UNAVAILABLE, "ya no están disponibles"),
        (ReviewChannelAvailability.UNCERTAIN, "No pude confirmar"),
    ],
)
def test_wrong_channel_message_omits_references_when_no_destination_is_visible(
    availability: ReviewChannelAvailability,
    expected: str,
) -> None:
    message = build_wrong_channel_message(
        channel_ids=(),
        message_id=1,
        availability=availability,
    )

    assert expected in message
    assert "<#" not in message


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
async def test_wrong_channel_reply_to_bot_replies_once_without_running_agent(
    settings,
    monkeypatch,
) -> None:
    replies: list[dict[str, object]] = []

    async def reply(**kwargs) -> None:
        replies.append(kwargs)

    bot = SimpleNamespace(
        user=SimpleNamespace(
            id=999,
            display_avatar=SimpleNamespace(url="https://cdn.discordapp.com/bot.png"),
        )
    )
    channels = [FakeReviewChannel(channel_id) for channel_id in settings.review_channel_ids or ()]
    guild = FakeGuild(
        min(settings.discord_guild_ids or ()),
        bot_member=bot.user,
        channels=channels,
    )
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.cog.discord.TextChannel",
        FakeReviewChannel,
    )
    cog = ReviewCog(bot, settings, UnexpectedReviewService())  # type: ignore[arg-type]
    message = SimpleNamespace(
        id=42,
        guild=guild,
        author=SimpleNamespace(id=123, bot=False),
        webhook_id=None,
        channel=SimpleNamespace(id=555),
        raw_mentions=[],
        reference=SimpleNamespace(
            type=discord.MessageReferenceType.default,
            resolved=SimpleNamespace(author=SimpleNamespace(id=999)),
            cached_message=None,
        ),
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
async def test_deleted_review_channels_are_omitted_and_cached(
    settings,
    monkeypatch,
) -> None:
    replies: list[dict[str, object]] = []

    async def reply(**kwargs) -> None:
        replies.append(kwargs)

    bot = SimpleNamespace(user=SimpleNamespace(id=999))
    guild = FakeGuild(
        min(settings.discord_guild_ids or ()),
        bot_member=bot.user,
        missing=True,
    )
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.cog.discord.TextChannel",
        FakeReviewChannel,
    )
    cog = ReviewCog(bot, settings, UnexpectedReviewService())  # type: ignore[arg-type]
    message = SimpleNamespace(
        id=43,
        guild=guild,
        author=SimpleNamespace(id=123, bot=False),
        webhook_id=None,
        channel=SimpleNamespace(id=555),
        raw_mentions=[999],
        reference=None,
        reply=reply,
    )

    await cog.on_message(message)  # type: ignore[arg-type]
    await cog.on_message(message)  # type: ignore[arg-type]

    assert len(guild.fetches) == len(settings.review_channel_ids or ())
    assert len(replies) == 1
    content = replies[0]["content"]
    assert isinstance(content, str)
    assert "ya no están disponibles" in content
    assert "<#" not in content


@pytest.mark.asyncio
async def test_hidden_review_channels_are_not_disclosed(
    settings,
    monkeypatch,
) -> None:
    replies: list[dict[str, object]] = []

    async def reply(**kwargs) -> None:
        replies.append(kwargs)

    bot = SimpleNamespace(user=SimpleNamespace(id=999))
    channels = [
        FakeReviewChannel(channel_id, hidden_user_ids={123})
        for channel_id in settings.review_channel_ids or ()
    ]
    guild = FakeGuild(
        min(settings.discord_guild_ids or ()),
        bot_member=bot.user,
        channels=channels,
    )
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.cog.discord.TextChannel",
        FakeReviewChannel,
    )
    cog = ReviewCog(bot, settings, UnexpectedReviewService())  # type: ignore[arg-type]
    message = SimpleNamespace(
        id=44,
        guild=guild,
        author=SimpleNamespace(id=123, bot=False),
        webhook_id=None,
        channel=SimpleNamespace(id=555),
        raw_mentions=[999],
        reference=None,
        reply=reply,
    )

    await cog.on_message(message)  # type: ignore[arg-type]

    content = replies[0]["content"]
    assert isinstance(content, str)
    assert "No tiene acceso visible" in content
    assert "<#" not in content


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
