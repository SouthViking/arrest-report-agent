"""Pure Discord trigger policy."""

from __future__ import annotations

from enum import StrEnum

import discord

from arrest_report_agent.config import AppSettings


class TriggerDecision(StrEnum):
    """Deterministic outcome for an incoming Discord message."""

    REVIEW = "review"
    REDIRECT_WRONG_CHANNEL = "redirect_wrong_channel"
    REDIRECT_UNAUTHORIZED_USER = "redirect_unauthorized_user"
    IGNORE_UNMENTIONED = "ignore_unmentioned"
    IGNORE_BOT_OR_WEBHOOK = "ignore_bot_or_webhook"
    IGNORE_WRONG_GUILD = "ignore_wrong_guild"


def configured_parent_channel_id(message: discord.Message) -> int | None:
    channel = message.channel
    if isinstance(channel, discord.Thread):
        return channel.parent_id
    return getattr(channel, "id", None)


def classify_message(
    message: discord.Message,
    bot_user_id: int,
    settings: AppSettings,
) -> TriggerDecision:
    """Classify mentions without invoking the agent or performing side effects."""

    if message.guild is None or message.guild.id != settings.discord_guild_id:
        return TriggerDecision.IGNORE_WRONG_GUILD
    if message.author.bot or message.webhook_id is not None:
        return TriggerDecision.IGNORE_BOT_OR_WEBHOOK
    if bot_user_id not in message.raw_mentions:
        return TriggerDecision.IGNORE_UNMENTIONED
    if not settings.is_user_authorized(message.author.id):
        return TriggerDecision.REDIRECT_UNAUTHORIZED_USER
    if configured_parent_channel_id(message) not in settings.review_channel_ids:
        return TriggerDecision.REDIRECT_WRONG_CHANNEL
    return TriggerDecision.REVIEW
