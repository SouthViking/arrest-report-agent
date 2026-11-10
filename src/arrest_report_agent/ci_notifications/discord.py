"""Durable GitHub Actions failure notifications delivered through Discord."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

import discord

from arrest_report_agent.config import AccessPolicyKey, AccessPolicyMode, BotPersona
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider
from arrest_report_agent.release import ReleaseManifest

from .repository import (
    CiDeliveryRecord,
    CiDeliveryTarget,
    CiNotificationRepository,
    CiWorkflowEventRecord,
)

LOGGER = logging.getLogger(__name__)
POLL_SECONDS = 30.0


class CiNotificationWorker:
    """Turn stored CI failures into idempotent Discord alerts."""

    def __init__(
        self,
        bot: discord.Bot,
        provider: DatabaseConfigProvider,
        repository: CiNotificationRepository,
        manifest: ReleaseManifest,
    ) -> None:
        self.bot = bot
        self.provider = provider
        self.repository = repository
        self.manifest = manifest
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._processing = asyncio.Lock()

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="ci-failure-notifications")

    async def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def wake(self) -> None:
        self._wake.set()

    async def process_once(self) -> None:
        async with self._processing:
            for event in await self.repository.pending_events():
                await self._prepare_event(event)
            if not self.bot.is_ready():
                return
            for delivery in await self.repository.due_deliveries():
                await self._deliver(delivery)

    async def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                await self.process_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log_event(
                    LOGGER,
                    "error",
                    "ci_notification_worker_failed",
                    exc_info=error,
                    **exception_fields(error),
                )
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=POLL_SECONDS)

    async def _prepare_event(self, event: CiWorkflowEventRecord) -> None:
        targets: list[CiDeliveryTarget] = []
        for guild_id in await self.provider.guild_ids():
            bundle = await self.provider.cached(guild_id)
            policy = bundle.snapshot.policies.get(
                AccessPolicyKey.CI_FAILURE_NOTIFICATION_CHANNELS
            )
            if policy is None or policy.mode is not AccessPolicyMode.RESTRICTED:
                continue
            for channel_id in policy.ids:
                targets.append(
                    CiDeliveryTarget(
                        guild_id=guild_id,
                        channel_id=channel_id,
                        notification_key=(
                            f"ci-failure:{guild_id}:{channel_id}:{event.repository_id}:"
                            f"{event.run_id}:{event.run_attempt}"
                        ),
                    )
                )
        if not targets:
            await self.repository.mark_event_ignored(event.id, "no_configured_targets")
            return
        await self.repository.enqueue_deliveries(event.id, tuple(targets))
        await self.repository.mark_event_processed(event.id)

    async def _deliver(self, delivery: CiDeliveryRecord) -> None:
        try:
            bundle = await self.provider.cached(delivery.guild_id)
            channel = await self._resolve_channel(delivery.guild_id, delivery.channel_id)
            message = await channel.send(
                embed=build_ci_failure_embed(
                    delivery.event,
                    self.manifest,
                    bundle.persona.profile,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as error:
            retryable = not isinstance(error, (discord.Forbidden, discord.NotFound, ValueError))
            await self.repository.mark_delivery_failed(
                delivery.id,
                attempts=delivery.attempts,
                error=f"{type(error).__name__}: {error}",
                retryable=retryable,
            )
            log_event(
                LOGGER,
                "warning" if retryable else "error",
                "ci_notification_delivery_failed",
                exc_info=error,
                guild_id=delivery.guild_id,
                channel_id=delivery.channel_id,
                run_id=delivery.event.run_id,
                retryable=retryable,
                **exception_fields(error),
            )
            return
        await self.repository.mark_delivery_sent(delivery.id, message.id)
        log_event(
            LOGGER,
            "info",
            "ci_notification_sent",
            guild_id=delivery.guild_id,
            channel_id=delivery.channel_id,
            message_id=message.id,
            run_id=delivery.event.run_id,
            run_attempt=delivery.event.run_attempt,
        )

    async def _resolve_channel(self, guild_id: int, channel_id: int) -> discord.TextChannel:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            raise RuntimeError("Configured guild is not available in the Discord cache")
        cached = guild.get_channel(channel_id)
        resolved = cached if cached is not None else await guild.fetch_channel(channel_id)
        if not isinstance(resolved, discord.TextChannel):
            raise ValueError("CI notification target must be a text channel")
        member = guild.me
        if member is None:
            raise RuntimeError("Bot member is not available in the Discord cache")
        permissions = resolved.permissions_for(member)
        if not (permissions.view_channel and permissions.send_messages and permissions.embed_links):
            raise ValueError("Missing permissions in CI notification channel")
        return resolved


def build_ci_failure_embed(
    event: CiWorkflowEventRecord,
    manifest: ReleaseManifest,
    persona: BotPersona,
) -> discord.Embed:
    """Render a safe operational alert with personality-aware framing."""

    embed = discord.Embed(
        title="🚨 Falló la integración continua",
        description=persona.messages.interface.deployments.ci_failed_description,
        color=discord.Color.red(),
        timestamp=event.timestamp,
    )
    origin = {
        "push": "Push",
        "pull_request": "Pull request",
        "workflow_dispatch": "Ejecución manual",
    }.get(
        event.triggering_event,
        discord.utils.escape_markdown(event.triggering_event),
    )
    embed.add_field(name="Repositorio", value=f"`{event.repository_full_name}`", inline=True)
    workflow = discord.utils.escape_markdown(event.workflow_name)
    embed.add_field(name="Workflow", value=f"`{workflow}`", inline=True)
    embed.add_field(name="Origen", value=origin, inline=True)
    if event.branch:
        branch = discord.utils.escape_markdown(event.branch)
        embed.add_field(name="Rama", value=f"`{branch}`", inline=True)
    repository_url = f"https://github.com/{event.repository_full_name}"
    commit_url = f"{repository_url}/commit/{event.commit_hash}"
    commit_label = event.commit_hash[:8]
    if event.commit_message:
        first_line = discord.utils.escape_markdown(event.commit_message.splitlines()[0][:100])
        commit_label = f"{commit_label} — {first_line}"
    embed.add_field(
        name="Commit",
        value=f"[{commit_label}]({commit_url})",
        inline=False,
    )
    embed.add_field(name="Ejecutado por", value=f"`{event.actor_login}`", inline=True)
    embed.add_field(
        name="Resultado",
        value=f"❌ Fallido · Intento {event.run_attempt}",
        inline=True,
    )
    run_url = f"{repository_url}/actions/runs/{event.run_id}"
    embed.add_field(
        name="Ejecución",
        value=f"[Revisar en GitHub Actions]({run_url})",
        inline=False,
    )
    embed.set_footer(text="GitHub Actions")
    return embed
