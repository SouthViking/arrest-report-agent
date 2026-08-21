"""Poll SARP and publish new vehicle-theft reports in configured channels."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass

import discord

from arrest_report_agent.config import AccessPolicyKey, AccessPolicyMode
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider, GuildRuntimeBundle
from arrest_report_agent.infrastructure.sarp import SarpStolenVehicle

from .discord import build_stolen_vehicle_alert_embed
from .models import StolenVehicleStatusFilter
from .repository import StolenVehicleFeedRepository
from .service import StolenVehicleService

LOGGER = logging.getLogger(__name__)
PAGE_SIZE = 100
MAX_CATCHUP_PAGES = 5


@dataclass(frozen=True, slots=True)
class StolenVehicleFeedTarget:
    guild_id: int
    channel_id: int


class StolenVehicleFeedWorker:
    def __init__(
        self,
        bot: discord.Bot,
        provider: DatabaseConfigProvider,
        repository: StolenVehicleFeedRepository,
        service: StolenVehicleService,
        *,
        poll_seconds: float,
        enabled_guild_ids: tuple[int, ...] = (),
    ) -> None:
        if poll_seconds < 10:
            raise ValueError("Stolen-vehicle poll interval must be at least 10 seconds")
        self.bot = bot
        self.provider = provider
        self.repository = repository
        self.service = service
        self.poll_seconds = poll_seconds
        self._enabled_guild_ids = set(enabled_guild_ids)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._processing = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running or not self._enabled_guild_ids:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="sarp-stolen-vehicle-feed")
        log_event(
            LOGGER,
            "info",
            "stolen_vehicle_feed_started",
            configured_guilds=len(self._enabled_guild_ids),
            poll_seconds=self.poll_seconds,
        )

    async def close(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def configuration_updated(self, bundle: GuildRuntimeBundle) -> None:
        enabled = self._bundle_enabled(bundle)
        if enabled:
            self._enabled_guild_ids.add(bundle.snapshot.guild_id)
            self.start()
            return
        self._enabled_guild_ids.discard(bundle.snapshot.guild_id)
        if not self._enabled_guild_ids:
            await self.close()
            log_event(LOGGER, "info", "stolen_vehicle_feed_stopped_no_targets")

    async def process_once(self) -> None:
        async with self._processing:
            targets = await self._targets()
            if not targets or not self.bot.is_ready():
                return
            first_page = await self.service.list_page(
                page=1,
                limit=PAGE_SIZE,
                status=StolenVehicleStatusFilter.ACTIVE,
            )
            newest_id = max((item.id for item in first_page.items), default=0)
            existing: dict[StolenVehicleFeedTarget, int] = {}
            for target in targets:
                cursor = await self.repository.get(target.guild_id, target.channel_id)
                if cursor is None:
                    created = await self.repository.initialize(
                        target.guild_id,
                        target.channel_id,
                        newest_id,
                    )
                    if created:
                        log_event(
                            LOGGER,
                            "info",
                            "stolen_vehicle_cursor_initialized",
                            guild_id=target.guild_id,
                            channel_id=target.channel_id,
                            last_report_id=newest_id,
                        )
                        continue
                    cursor = await self.repository.get(target.guild_id, target.channel_id)
                    if cursor is None:
                        raise RuntimeError(
                            "Stolen-vehicle cursor disappeared during initialization"
                        )
                existing[target] = cursor.last_report_id
            if not existing or not first_page.items:
                return

            reports = {item.id: item for item in first_page.items}
            minimum_cursor = min(existing.values())
            page_number = 1
            while (
                min(reports) > minimum_cursor
                and page_number < first_page.pages
                and page_number < MAX_CATCHUP_PAGES
            ):
                page_number += 1
                page = await self.service.list_page(
                    page=page_number,
                    limit=PAGE_SIZE,
                    status=StolenVehicleStatusFilter.ACTIVE,
                )
                reports.update((item.id, item) for item in page.items)
                if not page.items:
                    break
            if min(reports) > minimum_cursor and page_number < first_page.pages:
                log_event(
                    LOGGER,
                    "error",
                    "stolen_vehicle_backlog_exceeds_safe_window",
                    oldest_fetched_report_id=min(reports),
                    cursor_report_id=minimum_cursor,
                    fetched_pages=page_number,
                )
                return

            ordered = tuple(reports[report_id] for report_id in sorted(reports))
            for target, cursor_id in existing.items():
                await self._deliver_target(
                    target,
                    tuple(item for item in ordered if item.id > cursor_id),
                )

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.process_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log_event(
                    LOGGER,
                    "warning",
                    "stolen_vehicle_feed_poll_failed",
                    exc_info=error,
                    **exception_fields(error),
                )
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)

    async def _targets(self) -> tuple[StolenVehicleFeedTarget, ...]:
        targets: list[StolenVehicleFeedTarget] = []
        for guild_id in tuple(sorted(self._enabled_guild_ids)):
            bundle = await self.provider.cached(guild_id)
            if not self._bundle_enabled(bundle):
                continue
            policy = bundle.snapshot.policy(AccessPolicyKey.STOLEN_VEHICLE_CHANNELS)
            targets.extend(
                StolenVehicleFeedTarget(guild_id=guild_id, channel_id=channel_id)
                for channel_id in policy.ids
            )
        return tuple(targets)

    @staticmethod
    def _bundle_enabled(bundle: GuildRuntimeBundle) -> bool:
        feature = bundle.snapshot.features.get("stolen_vehicles")
        policy = bundle.snapshot.policies.get(AccessPolicyKey.STOLEN_VEHICLE_CHANNELS)
        return bool(
            feature is not None
            and feature.enabled
            and policy is not None
            and policy.mode is AccessPolicyMode.RESTRICTED
            and policy.ids
        )

    async def _deliver_target(
        self,
        target: StolenVehicleFeedTarget,
        reports: tuple[SarpStolenVehicle, ...],
    ) -> None:
        if not reports:
            return
        try:
            channel = await self._resolve_channel(target)
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "stolen_vehicle_channel_unavailable",
                exc_info=error,
                guild_id=target.guild_id,
                channel_id=target.channel_id,
                **exception_fields(error),
            )
            return
        for report in reports:
            try:
                message = await channel.send(
                    embed=build_stolen_vehicle_alert_embed(report),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                await self.repository.advance(
                    target.guild_id,
                    target.channel_id,
                    report.id,
                )
            except Exception as error:
                log_event(
                    LOGGER,
                    "warning",
                    "stolen_vehicle_delivery_failed",
                    exc_info=error,
                    guild_id=target.guild_id,
                    channel_id=target.channel_id,
                    report_id=report.id,
                    **exception_fields(error),
                )
                return
            log_event(
                LOGGER,
                "info",
                "stolen_vehicle_delivered",
                guild_id=target.guild_id,
                channel_id=target.channel_id,
                report_id=report.id,
                message_id=message.id,
            )

    async def _resolve_channel(
        self,
        target: StolenVehicleFeedTarget,
    ) -> discord.TextChannel:
        guild = self.bot.get_guild(target.guild_id)
        if guild is None:
            raise RuntimeError("Configured guild is unavailable")
        cached = guild.get_channel(target.channel_id)
        resolved = cached if cached is not None else await guild.fetch_channel(target.channel_id)
        if not isinstance(resolved, discord.TextChannel):
            raise ValueError("Stolen-vehicle destination must be a text channel")
        member = guild.me
        if member is None:
            raise RuntimeError("Bot member is unavailable")
        permissions = resolved.permissions_for(member)
        if not (permissions.view_channel and permissions.send_messages and permissions.embed_links):
            raise ValueError("Missing permissions in stolen-vehicle channel")
        return resolved
