"""Poll SARP and publish new service calls in configured Discord channels."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass

import discord

from arrest_report_agent.config import AccessPolicyKey, AccessPolicyMode
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider, GuildRuntimeBundle
from arrest_report_agent.infrastructure.sarp import SarpClient, SarpEmergencyCall

from .repository import ServiceCallRepository

LOGGER = logging.getLogger(__name__)
PAGE_SIZE = 100
MAX_CATCHUP_PAGES = 5


@dataclass(frozen=True, slots=True)
class ServiceCallTarget:
    guild_id: int
    channel_id: int


class ServiceCallFeedWorker:
    """Run only while at least one guild has a configured service-call target."""

    def __init__(
        self,
        bot: discord.Bot,
        provider: DatabaseConfigProvider,
        repository: ServiceCallRepository,
        client: SarpClient,
        *,
        poll_seconds: float,
        enabled_guild_ids: tuple[int, ...] = (),
    ) -> None:
        if poll_seconds < 10:
            raise ValueError("Service-call poll interval must be at least 10 seconds")
        self.bot = bot
        self.provider = provider
        self.repository = repository
        self.client = client
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
        self._task = asyncio.create_task(self._run(), name="sarp-service-call-feed")
        log_event(
            LOGGER,
            "info",
            "service_call_feed_started",
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
            log_event(LOGGER, "info", "service_call_feed_stopped_no_targets")

    async def process_once(self) -> None:
        async with self._processing:
            targets = await self._targets()
            if not targets or not self.bot.is_ready():
                return

            first_page = await self.client.list_emergency_calls(page=1, limit=PAGE_SIZE)
            if not first_page.items:
                return
            newest_id = max(call.id for call in first_page.items)
            existing: dict[ServiceCallTarget, int] = {}
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
                            "service_call_cursor_initialized",
                            guild_id=target.guild_id,
                            channel_id=target.channel_id,
                            last_call_id=newest_id,
                        )
                        continue
                    cursor = await self.repository.get(target.guild_id, target.channel_id)
                    if cursor is None:
                        raise RuntimeError("Service-call cursor disappeared during initialization")
                existing[target] = cursor.last_call_id
            if not existing:
                return

            calls = {call.id: call for call in first_page.items}
            minimum_cursor = min(existing.values())
            page_number = 1
            while (
                min(calls) > minimum_cursor
                and page_number < first_page.pages
                and page_number < MAX_CATCHUP_PAGES
            ):
                page_number += 1
                page = await self.client.list_emergency_calls(
                    page=page_number,
                    limit=PAGE_SIZE,
                )
                calls.update((call.id, call) for call in page.items)
                if not page.items:
                    break
            if min(calls) > minimum_cursor and page_number < first_page.pages:
                log_event(
                    LOGGER,
                    "error",
                    "service_call_backlog_exceeds_safe_window",
                    oldest_fetched_call_id=min(calls),
                    cursor_call_id=minimum_cursor,
                    fetched_pages=page_number,
                )
                return

            ordered_calls = tuple(calls[call_id] for call_id in sorted(calls))
            for target, cursor_id in existing.items():
                await self._deliver_target(
                    target,
                    tuple(call for call in ordered_calls if call.id > cursor_id),
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
                    "service_call_feed_poll_failed",
                    exc_info=error,
                    **exception_fields(error),
                )
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)

    async def _targets(self) -> tuple[ServiceCallTarget, ...]:
        targets: list[ServiceCallTarget] = []
        for guild_id in tuple(sorted(self._enabled_guild_ids)):
            bundle = await self.provider.cached(guild_id)
            if not self._bundle_enabled(bundle):
                continue
            policy = bundle.snapshot.policy(AccessPolicyKey.SERVICE_CALL_CHANNELS)
            targets.extend(
                ServiceCallTarget(guild_id=guild_id, channel_id=channel_id)
                for channel_id in policy.ids
            )
        return tuple(targets)

    @staticmethod
    def _bundle_enabled(bundle: GuildRuntimeBundle) -> bool:
        feature = bundle.snapshot.features.get("service_calls")
        policy = bundle.snapshot.policies.get(AccessPolicyKey.SERVICE_CALL_CHANNELS)
        return bool(
            feature is not None
            and feature.enabled
            and policy is not None
            and policy.mode is AccessPolicyMode.RESTRICTED
            and policy.ids
        )

    async def _deliver_target(
        self,
        target: ServiceCallTarget,
        calls: tuple[SarpEmergencyCall, ...],
    ) -> None:
        if not calls:
            return
        try:
            channel = await self._resolve_channel(target)
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "service_call_channel_unavailable",
                exc_info=error,
                guild_id=target.guild_id,
                channel_id=target.channel_id,
                **exception_fields(error),
            )
            return
        for call in calls:
            try:
                message = await channel.send(
                    embed=build_service_call_embed(call),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                await self.repository.advance(
                    target.guild_id,
                    target.channel_id,
                    call.id,
                )
            except Exception as error:
                log_event(
                    LOGGER,
                    "warning",
                    "service_call_delivery_failed",
                    exc_info=error,
                    guild_id=target.guild_id,
                    channel_id=target.channel_id,
                    call_id=call.id,
                    **exception_fields(error),
                )
                return
            log_event(
                LOGGER,
                "info",
                "service_call_delivered",
                guild_id=target.guild_id,
                channel_id=target.channel_id,
                call_id=call.id,
                message_id=message.id,
            )

    async def _resolve_channel(self, target: ServiceCallTarget) -> discord.TextChannel:
        guild = self.bot.get_guild(target.guild_id)
        if guild is None:
            raise RuntimeError("Configured guild is unavailable")
        cached = guild.get_channel(target.channel_id)
        resolved = cached if cached is not None else await guild.fetch_channel(target.channel_id)
        if not isinstance(resolved, discord.TextChannel):
            raise ValueError("Service-call destination must be a text channel")
        member = guild.me
        if member is None:
            raise RuntimeError("Bot member is unavailable")
        permissions = resolved.permissions_for(member)
        if not (permissions.view_channel and permissions.send_messages and permissions.embed_links):
            raise ValueError("Missing permissions in service-call channel")
        return resolved


def build_service_call_embed(call: SarpEmergencyCall) -> discord.Embed:
    """Render IC-safe operational data without creating mentions."""

    service, emoji, color = _service_theme(call.service)
    title_prefix = " ".join(part for part in (emoji, service) if part)
    title = (
        f"{title_prefix} · Llamada #{call.id:05d}"
        if title_prefix
        else f"Llamada #{call.id:05d}"
    )
    embed = discord.Embed(
        title=title,
        description=f"🚨 **Información recibida**\n{_reported_information(call)}",
        color=color,
        timestamp=call.created_at,
    )
    embed.add_field(
        name="📍 Ubicación",
        value=_safe(call.trace or call.location or "No informada", limit=1024),
        inline=False,
    )
    embed.add_field(
        name="👤 Solicitante",
        value=_caller(call),
        inline=True,
    )
    embed.add_field(
        name="📞 Teléfono",
        value=_phone(call.phone_number),
        inline=True,
    )
    return embed


def _service_theme(service: str | None) -> tuple[str, str, discord.Color]:
    normalized = service.strip().upper() if service else ""
    safe_service = _safe(normalized, limit=30) if normalized else ""
    if normalized in {"LSPD", "LSSD"}:
        return "Policía", "🚓", discord.Color.blue()
    if normalized == "LSFD":
        return safe_service, "🚒", discord.Color.red()
    return safe_service, "", discord.Color.light_grey()


def _reported_information(call: SarpEmergencyCall) -> str:
    primary_location = call.trace or call.location
    candidates = (call.location, call.situation)
    unique: list[str] = []
    seen = {primary_location.strip().casefold()} if primary_location else set()
    for candidate in candidates:
        if candidate is None:
            continue
        key = candidate.strip().casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(_safe(candidate, limit=1000))
    return "\n\n".join(unique) or "No informada"


def _safe(value: str, *, limit: int) -> str:
    escaped = discord.utils.escape_mentions(discord.utils.escape_markdown(value.strip()))
    return escaped[:limit] or "No informado"


def _phone(value: int | str | None) -> str:
    if value is None:
        return "No informado"
    normalized = str(value).strip()
    return normalized if normalized and normalized != "0" else "No informado"


def _caller(call: SarpEmergencyCall) -> str:
    if str(call.phone_number).strip() == "0":
        return "Anónimo"
    return _safe(call.character_name or "No informado", limit=100)
