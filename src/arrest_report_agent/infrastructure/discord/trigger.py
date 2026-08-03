"""Pure Discord trigger policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import discord

from arrest_report_agent.config import AppSettings, SnowflakeAllowlist


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


@dataclass(frozen=True, slots=True)
class DiscordAccessPolicy:
    """Central pre-processing policy for every mention-driven interaction."""

    guilds: SnowflakeAllowlist
    channels: SnowflakeAllowlist
    users: SnowflakeAllowlist

    @classmethod
    def from_settings(cls, settings: AppSettings) -> DiscordAccessPolicy:
        return cls(
            guilds=settings.guild_allowlist,
            channels=settings.channel_allowlist,
            users=settings.user_allowlist,
        )

    def classify(self, message: discord.Message, bot_user_id: int) -> TriggerDecision:
        """Reject ineligible events before context reads, agent runs, or Discord tools."""

        if message.guild is None or not self.guilds.allows(message.guild.id):
            return TriggerDecision.IGNORE_WRONG_GUILD
        if message.author.bot or message.webhook_id is not None:
            return TriggerDecision.IGNORE_BOT_OR_WEBHOOK
        if bot_user_id not in message.raw_mentions:
            return TriggerDecision.IGNORE_UNMENTIONED
        if not self.users.allows(message.author.id):
            return TriggerDecision.REDIRECT_UNAUTHORIZED_USER
        channel_id = configured_parent_channel_id(message)
        if channel_id is None or not self.channels.allows(channel_id):
            return TriggerDecision.REDIRECT_WRONG_CHANNEL
        return TriggerDecision.REVIEW


def classify_message(
    message: discord.Message,
    bot_user_id: int,
    settings: AppSettings,
) -> TriggerDecision:
    """Compatibility helper for pure policy tests and one-off classification."""

    return DiscordAccessPolicy.from_settings(settings).classify(message, bot_user_id)
