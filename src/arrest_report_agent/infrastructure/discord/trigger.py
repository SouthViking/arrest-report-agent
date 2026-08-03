"""Pure Discord trigger policy."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

import discord

from arrest_report_agent.config import AppSettings, SnowflakeAllowlist
from arrest_report_agent.config.logging import exception_fields, log_event

LOGGER = logging.getLogger(__name__)


class TriggerDecision(StrEnum):
    """Deterministic outcome for an incoming Discord message."""

    REVIEW = "review"
    REDIRECT_WRONG_CHANNEL = "redirect_wrong_channel"
    REDIRECT_UNAUTHORIZED_USER = "redirect_unauthorized_user"
    IGNORE_NOT_ADDRESSED = "ignore_not_addressed"
    IGNORE_BOT_OR_WEBHOOK = "ignore_bot_or_webhook"
    IGNORE_WRONG_GUILD = "ignore_wrong_guild"


class TriggerActivation(StrEnum):
    """How an eligible human addressed the bot."""

    MENTION = "mention"
    BOT_REPLY = "bot_reply"


@dataclass(frozen=True, slots=True)
class TriggerEvaluation:
    decision: TriggerDecision
    activation: TriggerActivation | None = None


def configured_parent_channel_id(message: discord.Message) -> int | None:
    channel = message.channel
    if isinstance(channel, discord.Thread):
        return channel.parent_id
    return getattr(channel, "id", None)


@dataclass(frozen=True, slots=True)
class DiscordAccessPolicy:
    """Central pre-processing policy for every bot-addressed interaction."""

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

    def classify(
        self,
        message: discord.Message,
        bot_user_id: int,
        *,
        referenced_author_id: int | None = None,
    ) -> TriggerDecision:
        """Reject ineligible events before context reads, agent runs, or Discord tools."""

        if message.guild is None or not self.guilds.allows(message.guild.id):
            return TriggerDecision.IGNORE_WRONG_GUILD
        if message.author.bot or message.webhook_id is not None:
            return TriggerDecision.IGNORE_BOT_OR_WEBHOOK
        if referenced_author_id is None:
            referenced_author_id = _resolved_reference_author_id(message)
        if bot_user_id not in message.raw_mentions and referenced_author_id != bot_user_id:
            return TriggerDecision.IGNORE_NOT_ADDRESSED
        if not self.users.allows(message.author.id):
            return TriggerDecision.REDIRECT_UNAUTHORIZED_USER
        channel_id = configured_parent_channel_id(message)
        if channel_id is None or not self.channels.allows(channel_id):
            return TriggerDecision.REDIRECT_WRONG_CHANNEL
        return TriggerDecision.REVIEW

    async def classify_event(
        self,
        message: discord.Message,
        bot_user_id: int,
    ) -> TriggerDecision:
        """Compatibility wrapper returning only the deterministic decision."""

        return (await self.evaluate_event(message, bot_user_id)).decision

    async def evaluate_event(
        self,
        message: discord.Message,
        bot_user_id: int,
    ) -> TriggerEvaluation:
        """Classify and retain the activation needed for deterministic response routing."""

        decision = self.classify(message, bot_user_id)
        referenced_author_id = _resolved_reference_author_id(message)
        if decision not in {TriggerDecision.REVIEW, TriggerDecision.IGNORE_NOT_ADDRESSED}:
            return TriggerEvaluation(
                decision=decision,
                activation=_activation(message, bot_user_id, referenced_author_id),
            )

        reference = _reply_reference(message)
        if reference is None or _reference_was_resolved(reference):
            return TriggerEvaluation(
                decision=decision,
                activation=_activation(message, bot_user_id, referenced_author_id),
            )

        referenced_message_id = getattr(reference, "message_id", None)
        channel_id = getattr(message.channel, "id", None)
        referenced_channel_id = getattr(reference, "channel_id", channel_id)
        fetch_message = getattr(message.channel, "fetch_message", None)
        if referenced_message_id is None or referenced_channel_id != channel_id or fetch_message is None:
            return TriggerEvaluation(
                decision=decision,
                activation=_activation(message, bot_user_id, referenced_author_id),
            )

        try:
            referenced_message = await fetch_message(referenced_message_id)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as error:
            log_event(
                LOGGER,
                "warning",
                "reply_reference_unavailable",
                exc_info=error,
                guild_id=message.guild.id if message.guild else None,
                channel_id=channel_id,
                message_id=message.id,
                referenced_message_id=referenced_message_id,
                user_id=message.author.id,
                **exception_fields(error),
            )
            return TriggerEvaluation(
                decision=decision,
                activation=_activation(message, bot_user_id, referenced_author_id),
            )

        referenced_author_id = referenced_message.author.id
        decision = self.classify(
            message,
            bot_user_id,
            referenced_author_id=referenced_author_id,
        )
        return TriggerEvaluation(
            decision=decision,
            activation=_activation(message, bot_user_id, referenced_author_id),
        )


def _reply_reference(message: discord.Message) -> discord.MessageReference | None:
    reference = getattr(message, "reference", None)
    if reference is None:
        return None
    reference_type = getattr(reference, "type", None)
    if reference_type not in (None, discord.MessageReferenceType.default):
        return None
    return reference


def _resolved_reference_author_id(message: discord.Message) -> int | None:
    reference = _reply_reference(message)
    if reference is None:
        return None
    referenced_message = getattr(reference, "resolved", None)
    if referenced_message is None:
        referenced_message = getattr(reference, "cached_message", None)
    author = getattr(referenced_message, "author", None)
    return getattr(author, "id", None)


def _reference_was_resolved(reference: discord.MessageReference) -> bool:
    return (
        getattr(reference, "resolved", None) is not None
        or getattr(reference, "cached_message", None) is not None
    )


def _activation(
    message: discord.Message,
    bot_user_id: int,
    referenced_author_id: int | None,
) -> TriggerActivation | None:
    if referenced_author_id == bot_user_id:
        return TriggerActivation.BOT_REPLY
    if bot_user_id in message.raw_mentions:
        return TriggerActivation.MENTION
    return None


def classify_message(
    message: discord.Message,
    bot_user_id: int,
    settings: AppSettings,
) -> TriggerDecision:
    """Compatibility helper for pure policy tests and one-off classification."""

    return DiscordAccessPolicy.from_settings(settings).classify(message, bot_user_id)
