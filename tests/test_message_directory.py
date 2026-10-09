from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import discord
import pytest

from arrest_report_agent.application.ports import MessageLookupQuery
from arrest_report_agent.infrastructure.discord.message_directory import (
    DiscordGuildMessageDirectory,
    _compact_embed,
)


def _author(user_id: int, *, bot: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        display_name=f"Author {user_id}",
        name=f"author_{user_id}",
        bot=bot,
        roles=[],
    )


def _attachment(attachment_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=attachment_id,
        filename=f"file-{attachment_id}.png",
        url=f"https://cdn.discord.test/file-{attachment_id}.png",
        content_type="image/png",
        size=100,
        width=800,
        height=600,
    )


def _message(
    message_id: int,
    channel: object,
    *,
    bot: bool = False,
    attachments: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=message_id,
        channel=channel,
        author=_author(100 + message_id, bot=bot),
        content=f"Message {message_id}",
        created_at=datetime(2026, 8, 6, 12, message_id, tzinfo=UTC),
        edited_at=None,
        reference=None,
        embeds=[],
        attachments=[_attachment(500 + message_id)] if attachments else [],
        reactions=[],
    )


class FakeTextChannel:
    def __init__(
        self,
        channel_id: int,
        guild: object,
        *,
        can_read: bool = True,
    ) -> None:
        self.id = channel_id
        self.guild = guild
        self.can_read = can_read
        self.messages: list[SimpleNamespace] = []
        self.history_calls: list[dict[str, object]] = []

    def permissions_for(self, requester: object) -> SimpleNamespace:
        return SimpleNamespace(
            view_channel=self.can_read,
            read_message_history=self.can_read,
        )

    async def fetch_message(self, message_id: int) -> SimpleNamespace:
        return next(message for message in self.messages if message.id == message_id)

    async def history(self, **kwargs):
        self.history_calls.append(kwargs)
        messages = self.messages
        before = kwargs.get("before")
        after = kwargs.get("after")
        if before is not None:
            messages = [message for message in messages if message.id < before.id]
        if after is not None:
            messages = [message for message in messages if message.id > after.id]
        messages = sorted(
            messages,
            key=lambda message: message.id,
            reverse=not bool(kwargs.get("oldest_first")),
        )
        for message in messages[: int(kwargs["limit"])]:
            yield message


class FakeThread(FakeTextChannel):
    def __init__(
        self,
        channel_id: int,
        guild: object,
        *,
        private: bool,
    ) -> None:
        super().__init__(channel_id, guild)
        self.private = private

    def is_private(self) -> bool:
        return self.private


class FakeGuild:
    def __init__(self, guild_id: int, channel: object) -> None:
        self.id = guild_id
        self.channel = channel
        self.requester = SimpleNamespace(id=999)

    def get_member(self, user_id: int):
        return self.requester if user_id == self.requester.id else None

    async def fetch_member(self, user_id: int):
        raise AssertionError("requester should be cached")

    def get_channel_or_thread(self, channel_id: int):
        return self.channel if channel_id == self.channel.id else None

    async def fetch_channel(self, channel_id: int):
        raise AssertionError("channel should be cached")


@pytest.fixture
def readable_directory(monkeypatch):
    channel = FakeTextChannel(88, SimpleNamespace(id=77))
    guild = FakeGuild(77, channel)
    channel.guild = guild
    bot = SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == 77 else None)
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.message_directory.discord.TextChannel",
        FakeTextChannel,
    )
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.message_directory.discord.Thread",
        FakeThread,
    )
    return DiscordGuildMessageDirectory(bot), channel


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({}, ""),
        ({"image": {"url": "https://cdn.discord.test/image.png"}}, ""),
        ({"thumbnail": {"url": "https://cdn.discord.test/thumbnail.png"}}, ""),
        ({"type": "video", "video": {"url": "https://example.com/video.mp4"}}, ""),
        (
            {"title": "Una tarjeta sin pie", "description": "Contenido"},
            "Título: Una tarjeta sin pie\nContenido",
        ),
        ({"footer": None}, ""),
        ({"footer": {"text": "Información"}}, "Pie: Información"),
        ({"fields": [{"name": "Estado", "value": "Listo"}]}, "Estado: Listo"),
        ({"fields": [{"name": "", "value": "Sin nombre"}]}, "Detalle: Sin nombre"),
    ],
    ids=["empty", "image", "thumbnail", "video", "card", "null-footer", "footer", "fields", "nameless-field"],
)
def test_compact_embed_tolerates_optional_discord_fields(payload, expected) -> None:
    assert _compact_embed(discord.Embed.from_dict(payload)) == expected


@pytest.mark.asyncio
async def test_message_directory_reads_mixed_history_without_requiring_text(readable_directory) -> None:
    directory, channel = readable_directory
    channel.messages = [_message(message_id, channel) for message_id in range(1, 8)]
    image_message = channel.messages[1]
    image_message.content = ""
    image_message.attachments = [_attachment(502)]
    card = channel.messages[2]
    card.content = ""
    card.author = _author(103, bot=True)
    card.embeds = [discord.Embed(title="Contáctanos", description="Selecciona una opción.")]
    for message in channel.messages[3:]:
        message.content = ""
    card.components = [SimpleNamespace(type=discord.ComponentType.string_select)]
    channel.messages[4].stickers = [SimpleNamespace(id=1, name="Sticker")]
    channel.messages[5].poll = SimpleNamespace(question="Una encuesta")
    channel.messages[6].type = discord.MessageType.pins_add
    video_message = channel.messages[3]
    video_message.attachments = [_attachment(504)]
    video_message.attachments[0].filename = "clip.mp4"
    video_message.attachments[0].content_type = "video/mp4"
    query = MessageLookupQuery(
        channel_id=88,
        message_id=None,
        direction="latest",
        limit=10,
        author_ids=(),
        author_type="any",
        has_attachments=None,
    )

    result = await directory.lookup_messages(77, 999, 88, query)

    assert result.error is None
    assert [message.message_id for message in result.messages] == list(range(1, 8))
    assert result.messages[0].content == "Message 1"
    assert result.messages[1].content == ""
    assert result.messages[1].attachments[0].id == 502
    assert result.messages[2].embeds == ("Título: Contáctanos\nSelecciona una opción.",)
    assert result.messages[3].attachments[0].filename == "clip.mp4"
    assert result.messages[3].attachments[0].input_kind is None
    assert all(message.content == "" for message in result.messages[4:])


@pytest.mark.asyncio
async def test_attachment_lookup_does_not_serialize_message_content(readable_directory, monkeypatch) -> None:
    directory, channel = readable_directory
    message = _message(4, channel, attachments=True)
    message.content = ""
    message.embeds = [discord.Embed().set_image(url="https://cdn.discord.test/image.png")]
    channel.messages = [message]

    def unexpected_serialization(message):
        raise AssertionError("Attachment reads must not depend on message text or embeds")

    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.message_directory._message_profile",
        unexpected_serialization,
    )

    result = await directory.lookup_message_attachments(77, 999, 88, 88, 4, ())

    assert result.error is None
    assert [attachment.id for attachment in result.attachments] == [504]
    assert result.attachments[0].url == "https://cdn.discord.test/file-504.png"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["hidden-channel", "foreign-guild", "private-thread"])
async def test_attachment_lookup_preserves_permission_boundary(readable_directory, case) -> None:
    directory, channel = readable_directory
    if case == "hidden-channel":
        channel.can_read = False
        expected = "channel_forbidden"
    elif case == "foreign-guild":
        channel.guild = SimpleNamespace(id=78)
        expected = "channel_outside_guild"
    else:
        thread = FakeThread(88, channel.guild, private=True)
        channel.guild.channel = thread
        expected = "private_thread_forbidden"

    result = await directory.lookup_message_attachments(77, 999, 99, 88, 4, ())

    assert result.error == expected
    assert result.attachments == ()
    assert channel.history_calls == []


@pytest.mark.asyncio
async def test_message_directory_reads_filtered_around_window_and_keeps_anchor(
    monkeypatch,
) -> None:
    guild_placeholder = SimpleNamespace(id=77)
    channel = FakeTextChannel(88, guild_placeholder)
    guild = FakeGuild(77, channel)
    channel.guild = guild
    channel.messages = [
        _message(1, channel),
        _message(2, channel, bot=True),
        _message(3, channel, attachments=True),
        _message(4, channel, bot=True),
        _message(5, channel, attachments=True),
        _message(6, channel, bot=True, attachments=True),
        _message(7, channel),
    ]
    bot = SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == 77 else None)
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.message_directory.discord.TextChannel",
        FakeTextChannel,
    )
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.message_directory.discord.Thread",
        FakeThread,
    )
    directory = DiscordGuildMessageDirectory(bot)
    query = MessageLookupQuery(
        channel_id=88,
        message_id=4,
        direction="around",
        limit=5,
        author_ids=(),
        author_type="human",
        has_attachments=True,
    )

    result = await directory.lookup_messages(77, 999, 88, query)

    assert result.error is None
    assert [message.message_id for message in result.messages] == [3, 4, 5]
    assert result.messages[1].author_is_bot is True
    assert result.messages[0].attachments[0].filename == "file-503.png"
    assert len(channel.history_calls) == 2


@pytest.mark.asyncio
async def test_message_directory_rejects_channels_hidden_from_requester(monkeypatch) -> None:
    guild_placeholder = SimpleNamespace(id=77)
    channel = FakeTextChannel(88, guild_placeholder, can_read=False)
    guild = FakeGuild(77, channel)
    channel.guild = guild
    bot = SimpleNamespace(get_guild=lambda guild_id: guild)
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.message_directory.discord.TextChannel",
        FakeTextChannel,
    )
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.message_directory.discord.Thread",
        FakeThread,
    )
    directory = DiscordGuildMessageDirectory(bot)
    query = MessageLookupQuery(
        channel_id=88,
        message_id=None,
        direction="latest",
        limit=5,
        author_ids=(),
        author_type="any",
        has_attachments=None,
    )

    result = await directory.lookup_messages(77, 999, 88, query)

    assert result.error == "channel_forbidden"
    assert channel.history_calls == []


@pytest.mark.asyncio
async def test_message_directory_rejects_other_private_threads(monkeypatch) -> None:
    guild_placeholder = SimpleNamespace(id=77)
    thread = FakeThread(88, guild_placeholder, private=True)
    guild = FakeGuild(77, thread)
    thread.guild = guild
    bot = SimpleNamespace(get_guild=lambda guild_id: guild)
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.message_directory.discord.TextChannel",
        FakeTextChannel,
    )
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.message_directory.discord.Thread",
        FakeThread,
    )
    directory = DiscordGuildMessageDirectory(bot)
    query = MessageLookupQuery(
        channel_id=88,
        message_id=None,
        direction="latest",
        limit=5,
        author_ids=(),
        author_type="any",
        has_attachments=None,
    )

    result = await directory.lookup_messages(77, 999, 99, query)

    assert result.error == "private_thread_forbidden"
    assert thread.history_calls == []


@pytest.mark.asyncio
async def test_message_directory_never_reads_a_channel_from_another_guild(monkeypatch) -> None:
    foreign_guild = SimpleNamespace(id=78)
    channel = FakeTextChannel(88, foreign_guild)
    guild = FakeGuild(77, channel)
    bot = SimpleNamespace(get_guild=lambda guild_id: guild)
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.message_directory.discord.TextChannel",
        FakeTextChannel,
    )
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.message_directory.discord.Thread",
        FakeThread,
    )
    directory = DiscordGuildMessageDirectory(bot)
    query = MessageLookupQuery(
        channel_id=88,
        message_id=None,
        direction="latest",
        limit=5,
        author_ids=(),
        author_type="any",
        has_attachments=None,
    )

    result = await directory.lookup_messages(77, 999, 88, query)

    assert result.error == "channel_outside_guild"
    assert channel.history_calls == []


@pytest.mark.asyncio
async def test_message_directory_resolves_selected_attachments_from_readable_message(
    monkeypatch,
) -> None:
    guild_placeholder = SimpleNamespace(id=77)
    channel = FakeTextChannel(88, guild_placeholder)
    guild = FakeGuild(77, channel)
    channel.guild = guild
    message = _message(4, channel, attachments=True)
    message.attachments.append(_attachment(999))
    channel.messages = [message]
    bot = SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == 77 else None)
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.message_directory.discord.TextChannel",
        FakeTextChannel,
    )
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.message_directory.discord.Thread",
        FakeThread,
    )
    directory = DiscordGuildMessageDirectory(bot)

    result = await directory.lookup_message_attachments(
        guild_id=77,
        requester_user_id=999,
        current_channel_id=88,
        channel_id=88,
        message_id=4,
        attachment_ids=(999, 123),
    )

    assert result.error is None
    assert [attachment.id for attachment in result.attachments] == [999]
    assert result.attachments[0].url == "https://cdn.discord.test/file-999.png"
    assert result.attachments[0].input_kind == "image"
    assert result.missing_ids == (123,)
