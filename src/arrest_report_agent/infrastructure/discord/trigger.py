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
    REDIRECT_BLOCKED_USER = "redirect_blocked_user"
    REDIRECT_UNAUTHORIZED_USER = "redirect_unauthorized_user"
    IGNORE_NOT_ADDRESSED = "ignore_not_addressed"
    IGNORE_BOT_OR_WEBHOOK = "ignore_bot_or_webhook"
    IGNORE_UNSUPPORTED_MESSAGE_TYPE = "ignore_unsupported_message_type"
    IGNORE_WRONG_GUILD = "ignore_wrong_guild"


class TriggerActivation(StrEnum):
    """How an eligible human addressed the bot."""

    MENTION = "mention"
    BOT_REPLY = "bot_reply"


@dataclass(frozen=True, slots=True)
class TriggerEvaluation:
    decision: TriggerDecision
    activation: TriggerActivation | None = None


@dataclass(frozen=True, slots=True)
class DiscordAccessPolicy:
    """Central pre-processing policy for every bot-addressed interaction."""

    guilds: SnowflakeAllowlist
    users: SnowflakeAllowlist
    roles: SnowflakeAllowlist
    blocked_users: SnowflakeAllowlist

    @classmethod
    def from_settings(cls, settings: AppSettings) -> DiscordAccessPolicy:
        return cls(
            guilds=settings.guild_allowlist,
            users=settings.user_allowlist,
            roles=settings.agent_role_allowlist,
            blocked_users=getattr(
                settings,
                "agent_blocked_user_allowlist",
                SnowflakeAllowlist(ids=frozenset()),
            ),
        )

    def blocks_member(self, member: discord.abc.User) -> bool:
        """Deny an explicitly blocked user regardless of positive access policies."""

        return self.blocked_users.allows(member.id)

    def allows_member(self, member: discord.abc.User) -> bool:
        """Authorize when either the user policy or the role policy matches."""

        if self.blocks_member(member):
            return False
        if self.users.allows(member.id):
            return True
        if self.roles.ids is None:
            return True
        return any(getattr(role, "id", None) in self.roles.ids for role in getattr(member, "roles", ()))

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
        if getattr(message, "type", discord.MessageType.default) not in {
            discord.MessageType.default,
            discord.MessageType.reply,
        }:
            return TriggerDecision.IGNORE_UNSUPPORTED_MESSAGE_TYPE
        if message.author.bot or message.webhook_id is not None:
            return TriggerDecision.IGNORE_BOT_OR_WEBHOOK
        if referenced_author_id is None:
            referenced_author_id = _resolved_reference_author_id(message)
        if bot_user_id not in message.raw_mentions and referenced_author_id != bot_user_id:
            return TriggerDecision.IGNORE_NOT_ADDRESSED
        if self.blocks_member(message.author):
            return TriggerDecision.REDIRECT_BLOCKED_USER
        if not self.allows_member(message.author):
            return TriggerDecision.REDIRECT_UNAUTHORIZED_USER
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
        if decision is TriggerDecision.IGNORE_UNSUPPORTED_MESSAGE_TYPE:
            return TriggerEvaluation(decision=decision)
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
