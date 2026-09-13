"""Bounded Discord lookup for legacy plain-text arrest-review posts."""

from __future__ import annotations

import logging
from collections.abc import Collection
from datetime import datetime

import discord

from arrest_report_agent.application import PersonalReportStatus
from arrest_report_agent.config import SnowflakeAllowlist
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.discord.personal_reports import (
    status_from_trusted_reactions,
)
from arrest_report_agent.infrastructure.discord.supervisor_access import SupervisorRolePolicy

from .models import ArrestReportReviewReference, ArrestReviewSource

LOGGER = logging.getLogger(__name__)


class DiscordLegacyArrestReviewLookup:
    """Find legacy roots in every configured report channel."""

    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

    async def resolve_approvals(
        self,
        *,
        guild_id: int,
        references: Collection[ArrestReportReviewReference],
        after: datetime,
        authorized_role_ids: Collection[int] | None,
    ) -> dict[int, ArrestReportReviewReference]:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return {}
        expected = tuple(
            reference
            for reference in references
            if reference.source is ArrestReviewSource.LEGACY
            and reference.guild_id == guild_id
        )
        if not expected:
            return {}

        policy = SupervisorRolePolicy(
            SnowflakeAllowlist(
                ids=(
                    None
                    if authorized_role_ids is None
                    else frozenset(authorized_role_ids)
                )
            )
        )
        member_cache: dict[int, discord.Member | None] = {}
        found: dict[int, ArrestReportReviewReference] = {}
        by_channel: dict[int, dict[int, ArrestReportReviewReference]] = {}
        for reference in expected:
            by_channel.setdefault(reference.parent_channel_id, {})[
                reference.root_message_id
            ] = reference
        for channel in _report_channels(guild, by_channel):
            pending_roots = by_channel[channel.id]
            try:
                async for message in channel.history(
                    limit=None,
                    after=after,
                    oldest_first=False,
                ):
                    matched_reference = pending_roots.get(message.id)
                    if matched_reference is None or not _is_legacy_root(message):
                        continue
                    status = await status_from_trusted_reactions(
                        message,
                        policy,
                        member_cache,
                    )
                    found[matched_reference.report_id] = matched_reference.model_copy(
                        update={"approved": status is PersonalReportStatus.APPROVED}
                    )
                    pending_roots.pop(message.id)
                    if not pending_roots:
                        break
            except (discord.Forbidden, discord.HTTPException) as error:
                log_event(
                    LOGGER,
                    "warning",
                    "legacy_arrest_review_history_unavailable",
                    exc_info=error,
                    guild_id=guild.id,
                    channel_id=channel.id,
                    **exception_fields(error),
                )
        return found


def _report_channels(
    guild: discord.Guild,
    roots_by_channel: dict[int, dict[int, ArrestReportReviewReference]],
) -> tuple[discord.TextChannel, ...]:
    candidates: list[object]
    candidates = []
    for channel_id in sorted(roots_by_channel):
        channel = guild.get_channel(channel_id)
        if channel is not None:
            candidates.append(channel)
    bot_member = guild.me
    visible: list[discord.TextChannel] = []
    for candidate in candidates:
        if not isinstance(candidate, discord.TextChannel) or bot_member is None:
            continue
        if _can_read_history(candidate, bot_member):
            visible.append(candidate)
    return tuple(visible)


def _can_read_history(channel: discord.TextChannel, member: discord.Member) -> bool:
    permissions = channel.permissions_for(member)
    return permissions.view_channel and permissions.read_message_history


def _is_legacy_root(message: discord.Message) -> bool:
    return not message.author.bot and message.webhook_id is None and message.reference is None
