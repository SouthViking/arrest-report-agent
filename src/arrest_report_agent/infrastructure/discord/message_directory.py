"""Bounded, permission-aware Discord message lookup for agent tools."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import discord

from arrest_report_agent.application.ports import (
    GuildMessageProfile,
    MessageAttachmentLookupResult,
    MessageAttachmentProfile,
    MessageLookupQuery,
    MessageLookupResult,
    MessageReactionProfile,
)
from arrest_report_agent.config.logging import exception_fields, log_event

from .attachments import classify_attachment
from .member_directory import compact_role_names

MAX_HISTORY_SCAN = 50
MAX_EMBEDS_PER_MESSAGE = 3
MAX_EMBED_TEXT_CHARS = 3000
MAX_ATTACHMENTS_PER_MESSAGE = 10
MAX_REACTIONS_PER_MESSAGE = 20
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ReadableMessageWindow:
    messages: tuple[discord.Message, ...] = ()
    error: str | None = None


class DiscordGuildMessageDirectory:
    """Read messages from an agent-selected channel without crossing the active guild."""

    def __init__(self, bot: discord.Client) -> None:
        self.bot = bot

    async def lookup_messages(
        self,
        guild_id: int,
        requester_user_id: int,
        current_channel_id: int,
        query: MessageLookupQuery,
    ) -> MessageLookupResult:
        lookup = await self._lookup_readable_messages(
            guild_id,
            requester_user_id,
            current_channel_id,
            query,
        )
        if lookup.error is not None:
            return MessageLookupResult(messages=(), error=lookup.error)
        profiles = tuple(_message_profile(message) for message in lookup.messages)
        return MessageLookupResult(
            messages=profiles,
            truncated=any(profile.truncated for profile in profiles),
        )

    async def _lookup_readable_messages(
        self,
        guild_id: int,
        requester_user_id: int,
        current_channel_id: int,
        query: MessageLookupQuery,
    ) -> _ReadableMessageWindow:
        """Resolve permissions and native messages independently of their content or presentation."""

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return self._error("guild_unavailable", guild_id, query.channel_id)

        requester = guild.get_member(requester_user_id)
        if requester is None:
            try:
                requester = await guild.fetch_member(requester_user_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
                return self._error(
                    "requester_unavailable",
                    guild_id,
                    query.channel_id,
                    error,
                )

        channel = guild.get_channel_or_thread(query.channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(query.channel_id)
            except discord.InvalidData as error:
                return self._error("channel_outside_guild", guild_id, query.channel_id, error)
            except discord.NotFound as error:
                return self._error("channel_not_found", guild_id, query.channel_id, error)
            except (discord.Forbidden, discord.HTTPException) as error:
                return self._error("channel_unavailable", guild_id, query.channel_id, error)

        if channel.guild.id != guild_id:
            return self._error("channel_outside_guild", guild_id, query.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return self._error("unsupported_channel", guild_id, query.channel_id)

        permissions = channel.permissions_for(requester)
        if not permissions.view_channel or not permissions.read_message_history:
            return self._error("channel_forbidden", guild_id, query.channel_id)
        if isinstance(channel, discord.Thread) and channel.is_private() and channel.id != current_channel_id:
            return self._error("private_thread_forbidden", guild_id, query.channel_id)

        try:
            messages = await self._read_window(channel, query)
        except discord.NotFound as error:
            return self._error("message_not_found", guild_id, query.channel_id, error)
        except discord.Forbidden as error:
            return self._error("message_forbidden", guild_id, query.channel_id, error)
        except discord.HTTPException as error:
            return self._error("message_history_unavailable", guild_id, query.channel_id, error)

        return _ReadableMessageWindow(messages=tuple(messages))

    async def lookup_message_attachments(
        self,
        guild_id: int,
        requester_user_id: int,
        current_channel_id: int,
        channel_id: int,
        message_id: int,
        attachment_ids: tuple[int, ...],
    ) -> MessageAttachmentLookupResult:
        """Fetch fresh attachment URLs through the same permission boundary as message reads."""

        lookup = await self._lookup_readable_messages(
            guild_id=guild_id,
            requester_user_id=requester_user_id,
            current_channel_id=current_channel_id,
            query=MessageLookupQuery(
                channel_id=channel_id,
                message_id=message_id,
                direction="exact",
                limit=1,
                author_ids=(),
                author_type="any",
                has_attachments=None,
            ),
        )
        if lookup.error is not None:
            return MessageAttachmentLookupResult(
                attachments=(),
                error=lookup.error,
            )
        if not lookup.messages:
            return MessageAttachmentLookupResult(
                attachments=(),
                error="message_not_found",
            )

        available = {
            attachment.id: _attachment_profile(attachment)
            for attachment in lookup.messages[0].attachments[:MAX_ATTACHMENTS_PER_MESSAGE]
        }
        selected_ids = attachment_ids or tuple(available)
        return MessageAttachmentLookupResult(
            attachments=tuple(
                available[attachment_id] for attachment_id in selected_ids if attachment_id in available
            ),
            missing_ids=tuple(
                attachment_id for attachment_id in selected_ids if attachment_id not in available
            ),
        )

    async def _read_window(
        self,
        channel: discord.TextChannel | discord.Thread,
        query: MessageLookupQuery,
    ) -> list[discord.Message]:
        if query.direction == "latest":
            candidates = [
                message
                async for message in channel.history(
                    limit=_scan_limit(query.limit),
                    oldest_first=False,
                )
            ]
            selected = [message for message in candidates if _matches(message, query)][: query.limit]
            return _chronological(selected)

        assert query.message_id is not None
        anchor = await channel.fetch_message(query.message_id)
        if query.direction == "exact" or query.limit == 1:
            return [anchor]

        remaining = query.limit - 1
        if query.direction == "before":
            before = await _history_side(channel, anchor, remaining, query, before=True)
            return _chronological([*before, anchor])
        if query.direction == "after":
            after = await _history_side(channel, anchor, remaining, query, before=False)
            return _chronological([anchor, *after])

        before_count = remaining // 2
        after_count = remaining - before_count
        before = await _history_side(channel, anchor, before_count, query, before=True)
        after = await _history_side(channel, anchor, after_count, query, before=False)
        return _chronological([*before, anchor, *after])

    def _error(
        self,
        code: str,
        guild_id: int,
        channel_id: int,
        error: Exception | None = None,
    ) -> _ReadableMessageWindow:
        log_event(
            LOGGER,
            "warning",
            "message_lookup_failed",
            exc_info=error,
            guild_id=guild_id,
            channel_id=channel_id,
            lookup_error=code,
            **(exception_fields(error) if error is not None else {}),
        )
        return _ReadableMessageWindow(error=code)


async def _history_side(
    channel: discord.TextChannel | discord.Thread,
    anchor: discord.Message,
    count: int,
    query: MessageLookupQuery,
    *,
    before: bool,
) -> list[discord.Message]:
    if count <= 0:
        return []
    kwargs: dict[str, object] = {
        "limit": _scan_limit(count),
        "oldest_first": not before,
        "before" if before else "after": anchor,
    }
    candidates = [message async for message in channel.history(**kwargs)]
    return [message for message in candidates if _matches(message, query)][:count]


def _scan_limit(result_limit: int) -> int:
    return min(max(result_limit * 5, 20), MAX_HISTORY_SCAN)


def _matches(message: discord.Message, query: MessageLookupQuery) -> bool:
    if query.author_ids and message.author.id not in query.author_ids:
        return False
    if query.author_type == "human" and message.author.bot:
        return False
    if query.author_type == "bot" and not message.author.bot:
        return False
    return not (query.has_attachments is not None and bool(message.attachments) is not query.has_attachments)


def _chronological(messages: list[discord.Message]) -> list[discord.Message]:
    unique = {message.id: message for message in messages}
    return sorted(unique.values(), key=lambda message: (message.created_at, message.id))


def _message_profile(message: discord.Message) -> GuildMessageProfile:
    embeds = tuple(_compact_embed(embed) for embed in message.embeds[:MAX_EMBEDS_PER_MESSAGE])
    attachments = tuple(
        _attachment_profile(attachment) for attachment in message.attachments[:MAX_ATTACHMENTS_PER_MESSAGE]
    )
    reactions = tuple(
        MessageReactionProfile(
            emoji=str(reaction.emoji),
            count=reaction.count,
            reacted_by_bot=reaction.me,
        )
        for reaction in message.reactions[:MAX_REACTIONS_PER_MESSAGE]
    )
    reference_id = message.reference.message_id if message.reference is not None else None
    return GuildMessageProfile(
        message_id=message.id,
        channel_id=message.channel.id,
        author_id=message.author.id,
        author_display_name=getattr(message.author, "display_name", message.author.name),
        author_username=message.author.name,
        author_is_bot=message.author.bot,
        author_roles=tuple([] if message.author.bot else compact_role_names(message.author)),
        content=message.content,
        created_at=message.created_at,
        edited_at=message.edited_at,
        reply_to_message_id=reference_id,
        embeds=embeds,
        attachments=attachments,
        reactions=reactions,
        truncated=(
            len(message.embeds) > MAX_EMBEDS_PER_MESSAGE
            or len(message.attachments) > MAX_ATTACHMENTS_PER_MESSAGE
            or len(message.reactions) > MAX_REACTIONS_PER_MESSAGE
            or any(len(text) >= MAX_EMBED_TEXT_CHARS for text in embeds)
        ),
    )


def _attachment_profile(attachment: discord.Attachment) -> MessageAttachmentProfile:
    input_kind, content_type = classify_attachment(
        attachment.filename,
        attachment.content_type,
    )
    return MessageAttachmentProfile(
        id=attachment.id,
        filename=attachment.filename,
        url=attachment.url,
        content_type=content_type,
        size=attachment.size,
        width=attachment.width,
        height=attachment.height,
        input_kind=input_kind.value if input_kind is not None else None,
    )


def _compact_embed(embed: discord.Embed) -> str:
    lines: list[str] = []
    if embed.title:
        lines.append(f"Título: {embed.title}")
    if embed.description:
        lines.append(str(embed.description))
    for field in embed.fields:
        name = getattr(field, "name", None)
        value = getattr(field, "value", None)
        if name or value:
            lines.append(f"{name or 'Detalle'}: {value or ''}")
    footer_text = getattr(embed.footer, "text", None)
    if footer_text:
        lines.append(f"Pie: {footer_text}")
    text = "\n".join(lines)
    return text[: MAX_EMBED_TEXT_CHARS - 1] + "…" if len(text) > MAX_EMBED_TEXT_CHARS else text
