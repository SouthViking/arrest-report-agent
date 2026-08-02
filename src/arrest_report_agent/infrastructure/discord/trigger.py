"""Pure Discord trigger policy."""

from __future__ import annotations

import discord

from arrest_report_agent.config import AppSettings


def configured_parent_channel_id(message: discord.Message) -> int | None:
    channel = message.channel
    if isinstance(channel, discord.Thread):
        return channel.parent_id
    return getattr(channel, "id", None)


def should_review_message(message: discord.Message, bot_user_id: int, settings: AppSettings) -> bool:
    """Accept explicit mentions from humans in configured channels or their threads."""

    if message.guild is None or message.guild.id != settings.discord_guild_id:
        return False
    if message.author.bot or message.webhook_id is not None:
        return False
    if configured_parent_channel_id(message) not in settings.review_channel_ids:
        return False
    return bot_user_id in message.raw_mentions

