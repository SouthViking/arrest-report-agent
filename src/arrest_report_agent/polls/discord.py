"""Gateway listeners that keep non-authoritative poll activity metadata fresh."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import discord
from discord.ext import commands

from arrest_report_agent.config.logging import exception_fields, log_event

from .repository import PollRepository

LOGGER = logging.getLogger(__name__)


class PollEventCog(commands.Cog):
    def __init__(self, repository: PollRepository) -> None:
        self.repository = repository

    @commands.Cog.listener()
    async def on_raw_poll_vote_add(self, payload: discord.RawMessagePollVoteEvent) -> None:
        await self._touch(payload)

    @commands.Cog.listener()
    async def on_raw_poll_vote_remove(self, payload: discord.RawMessagePollVoteEvent) -> None:
        await self._touch(payload)

    async def _touch(self, payload: discord.RawMessagePollVoteEvent) -> None:
        if payload.guild_id is None:
            return
        try:
            await self.repository.touch_activity(
                payload.guild_id,
                payload.message_id,
                at=datetime.now(UTC),
            )
        except Exception as error:
            log_event(
                LOGGER,
                "warning",
                "agent_poll_vote_metadata_update_failed",
                exc_info=error,
                guild_id=payload.guild_id,
                channel_id=payload.channel_id,
                message_id=payload.message_id,
                **exception_fields(error),
            )
