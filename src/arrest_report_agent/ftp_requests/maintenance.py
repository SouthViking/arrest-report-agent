"""First consumer of maintenance: expire FTP requests and reconcile their cards."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from functools import partial

import discord

from arrest_report_agent.application.maintenance import MaintenanceScheduler
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider, GuildRuntimeBundle

from .discord import FtpRequestControlsView, build_ftp_request_embed
from .models import FtpTrainingRequest
from .repository import FtpRequestRepository

LOGGER = logging.getLogger(__name__)


class FtpRequestMaintenance:
    def __init__(
        self, bot: discord.Bot, provider: DatabaseConfigProvider,
        repository: FtpRequestRepository, scheduler: MaintenanceScheduler,
    ) -> None:
        self.bot = bot
        self.provider = provider
        self.repository = repository
        self.scheduler = scheduler

    async def configuration_updated(self, bundle: GuildRuntimeBundle) -> None:
        guild_id = bundle.snapshot.guild_id
        key = f"ftp-expiry:{guild_id}"
        options = bundle.settings.ftp_request_options
        if options is None:
            self.scheduler.unregister(key)
        else:
            self.scheduler.register(
                key, partial(self.process_guild, guild_id),
                interval_seconds=options.expiry_check_minutes * 60,
            )

    async def process_guild(self, guild_id: int, *, now: datetime | None = None) -> None:
        bundle = await self.provider.cached(guild_id)
        options = bundle.settings.ftp_request_options
        if options is None:
            return
        current = now or datetime.now(UTC)
        cancelled: tuple[FtpTrainingRequest, ...] = ()
        if options.expiry_enabled:
            cancelled = await self.repository.expire_pending(
                guild_id, cutoff=current - timedelta(hours=options.expiry_grace_hours), now=current,
            )
            for request in cancelled:
                log_event(
                    LOGGER, "info", "ftp_request_auto_cancelled", guild_id=guild_id,
                    request_id=str(request.id), message_id=request.message_id,
                    requester_user_id=request.requester_user_id,
                )
        # Persisted edits are still retried when automatic cancellation is switched off.
        requests = await self.repository.list_expiry_sync(guild_id, now=current)
        synced = 0
        for request in requests:
            try:
                await self._edit(request)
                await self.repository.set_expiry_sync_due(request, None)
                synced += 1
            except Exception as error:
                await self.repository.set_expiry_sync_due(
                    request, current + timedelta(minutes=options.expiry_check_minutes),
                )
                log_event(
                    LOGGER, "warning", "ftp_expiry_card_sync_failed", guild_id=guild_id,
                    request_id=str(request.id), channel_id=request.channel_id,
                    message_id=request.message_id, **exception_fields(error),
                )
        if cancelled or requests:
            log_event(
                LOGGER, "info", "ftp_expiry_completed", guild_id=guild_id,
                cancelled=len(cancelled), synced=synced, retrying=len(requests) - synced,
            )

    async def _edit(self, request: FtpTrainingRequest) -> None:
        if request.message_id is None:
            return
        guild = self.bot.get_guild(request.guild_id)
        if guild is None:
            raise LookupError("FTP request guild unavailable")
        try:
            channel = guild.get_channel(request.channel_id) or await guild.fetch_channel(request.channel_id)
            if not isinstance(channel, discord.TextChannel):
                raise LookupError("FTP request channel is not a text channel")
            message = await channel.fetch_message(request.message_id)
            if self.bot.user is None or message.author.id != self.bot.user.id:
                raise PermissionError("FTP request message is not owned by the bot")
            embed = build_ftp_request_embed(request)
            # Preserve the native author/avatar without an extra member lookup.
            if message.embeds and (author := message.embeds[0].to_dict().get("author")):
                embed.set_author(name=author["name"], icon_url=author.get("icon_url"))
            await message.edit(
                content=None, embed=embed, view=FtpRequestControlsView(disabled=True),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.NotFound:
            # Deletion is permanent: stop retrying, but keep the cancellation for audit.
            log_event(
                LOGGER, "warning", "ftp_expiry_card_missing", guild_id=request.guild_id,
                request_id=str(request.id), channel_id=request.channel_id, message_id=request.message_id,
            )
