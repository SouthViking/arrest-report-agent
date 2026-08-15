"""Durable Railway deployment notifications delivered through Discord."""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import suppress

import discord

from arrest_report_agent.config import AccessPolicyKey, AccessPolicyMode, BotPersona
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider
from arrest_report_agent.release import (
    ReleaseAcknowledgementType,
    ReleaseChangeType,
    ReleaseManifest,
)

from .models import DeploymentEventType
from .repository import (
    DeploymentDeliveryRecord,
    DeploymentDeliveryTarget,
    DeploymentEventRecord,
    DeploymentRepository,
)

LOGGER = logging.getLogger(__name__)
POLL_SECONDS = 30.0

CHANGE_ICONS = {
    ReleaseChangeType.ADDED: "\u2728",
    ReleaseChangeType.IMPROVED: "\U0001f527",
    ReleaseChangeType.FIXED: "\U0001f41b",
    ReleaseChangeType.SECURITY: "\U0001f512",
}
ACKNOWLEDGEMENT_ICONS = {
    ReleaseAcknowledgementType.SUGGESTION: "💡",
    ReleaseAcknowledgementType.TESTING: "🧪",
    ReleaseAcknowledgementType.CONTRIBUTION: "🤝",
}


class DeploymentNotificationWorker:
    """Turn durable events into idempotent Discord outbox deliveries."""

    def __init__(
        self,
        bot: discord.Bot,
        provider: DatabaseConfigProvider,
        repository: DeploymentRepository,
        manifest: ReleaseManifest,
        *,
        current_commit_sha: str | None,
    ) -> None:
        self.bot = bot
        self.provider = provider
        self.repository = repository
        self.manifest = manifest
        self.current_commit_sha = (current_commit_sha or "").strip()
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._processing = asyncio.Lock()

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="deployment-notifications")

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
                    "deployment_notification_worker_failed",
                    exc_info=error,
                    **exception_fields(error),
                )
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=POLL_SECONDS)

    async def _prepare_event(self, event: DeploymentEventRecord) -> None:
        if event.event_type is DeploymentEventType.DEPLOYED:
            if event.source is None or event.source.lower() != "github":
                await self.repository.mark_event_ignored(event.id, "unsupported_source")
                return
            if not re.fullmatch(r"[0-9a-fA-F]{7,64}", event.commit_hash or ""):
                await self.repository.mark_event_ignored(event.id, "unverifiable_commit")
                return
            if not self._event_matches_current_commit(event):
                # The old container receives the successful deployment event. The new
                # container processes it only once its own RAILWAY_GIT_COMMIT_SHA matches.
                return

        targets: list[DeploymentDeliveryTarget] = []
        for guild_id in await self.provider.guild_ids():
            bundle = await self.provider.cached(guild_id)
            feature = bundle.snapshot.features.get("deployment_notifications")
            if feature is None or not feature.enabled:
                continue
            policy = bundle.snapshot.policies.get(
                AccessPolicyKey.DEPLOYMENT_NOTIFICATION_CHANNELS
            )
            if policy is None or policy.mode is not AccessPolicyMode.RESTRICTED:
                continue
            channel_ids = policy.ids
            if event.event_type is DeploymentEventType.DEPLOYED:
                channel_ids = tuple(
                    channel_id
                    for channel_id in channel_ids
                    if self.manifest.targets_changelog_channel(channel_id)
                )
            for channel_id in channel_ids:
                kind = (
                    "release"
                    if event.event_type is DeploymentEventType.DEPLOYED
                    else "incident"
                )
                key = (
                    f"release:{guild_id}:{channel_id}:{self.manifest.version}"
                    if kind == "release"
                    else (
                        f"incident:{guild_id}:{channel_id}:"
                        f"{event.event_type.value}:{event.deployment_id}"
                    )
                )
                targets.append(
                    DeploymentDeliveryTarget(
                        guild_id=guild_id,
                        channel_id=channel_id,
                        kind=kind,
                        notification_key=key,
                    )
                )
        if not targets:
            await self.repository.mark_event_ignored(event.id, "no_configured_targets")
            return
        await self.repository.enqueue_deliveries(event.id, tuple(targets))
        await self.repository.mark_event_processed(event.id)

    def _event_matches_current_commit(self, event: DeploymentEventRecord) -> bool:
        event_sha = event.commit_hash or ""
        runtime_sha = self.current_commit_sha
        if len(event_sha) < 7 or len(runtime_sha) < 7:
            return False
        left, right = event_sha.lower(), runtime_sha.lower()
        return left.startswith(right) or right.startswith(left)

    async def _deliver(self, delivery: DeploymentDeliveryRecord) -> None:
        try:
            bundle = await self.provider.cached(delivery.guild_id)
            channel = await self._resolve_channel(delivery.guild_id, delivery.channel_id)
            embed = build_deployment_embed(
                delivery.event,
                self.manifest,
                bundle.persona.profile,
            )
            message = await channel.send(
                embed=embed,
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
                "deployment_notification_delivery_failed",
                exc_info=error,
                guild_id=delivery.guild_id,
                channel_id=delivery.channel_id,
                event_type=delivery.event.event_type.value,
                retryable=retryable,
                **exception_fields(error),
            )
            return
        await self.repository.mark_delivery_sent(delivery.id, message.id)
        log_event(
            LOGGER,
            "info",
            "deployment_notification_sent",
            guild_id=delivery.guild_id,
            channel_id=delivery.channel_id,
            message_id=message.id,
            event_type=delivery.event.event_type.value,
            release_version=(
                self.manifest.version
                if delivery.event.event_type is DeploymentEventType.DEPLOYED
                else None
            ),
        )

    async def _resolve_channel(
        self,
        guild_id: int,
        channel_id: int,
    ) -> discord.TextChannel:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            raise RuntimeError("Configured guild is not available in the Discord cache")
        cached_channel = guild.get_channel(channel_id)
        resolved_channel = (
            cached_channel
            if cached_channel is not None
            else await guild.fetch_channel(channel_id)
        )
        if not isinstance(resolved_channel, discord.TextChannel):
            raise ValueError("Deployment notification target must be a text channel")
        member = guild.me
        if member is None:
            raise RuntimeError("Bot member is not available in the Discord cache")
        permissions = resolved_channel.permissions_for(member)
        if not (
            permissions.view_channel
            and permissions.send_messages
            and permissions.embed_links
        ):
            raise ValueError("Missing permissions in deployment notification channel")
        return resolved_channel


def build_deployment_embed(
    event: DeploymentEventRecord,
    manifest: ReleaseManifest,
    persona: BotPersona,
) -> discord.Embed:
    """Render one neutral operational embed with persona-specific framing."""

    messages = persona.messages.interface.deployments
    if event.event_type is DeploymentEventType.DEPLOYED:
        embed = discord.Embed(
            title=f"\U0001f680 {manifest.title} \u00b7 v{manifest.version}",
            description=f"{messages.release_intro}\n\n{manifest.summary}",
            color=discord.Color.green(),
            timestamp=event.timestamp,
        )
        changes = "\n".join(
            f"{CHANGE_ICONS[change.type]} {change.text}" for change in manifest.changes
        )
        embed.add_field(name="Cambios", value=changes, inline=False)
        if manifest.acknowledgements:
            acknowledgements = "\n".join(
                f"{ACKNOWLEDGEMENT_ICONS[item.type]} <@{item.user_id}> — {item.text}"
                for item in manifest.acknowledgements
            )
            embed.add_field(
                name="Agradecimientos",
                value=acknowledgements,
                inline=False,
            )
        embed.set_footer(text=messages.release_closing)
    else:
        title, description, color = {
            DeploymentEventType.FAILED: (
                "\u26a0\ufe0f Despliegue fallido",
                messages.failed_description,
                discord.Color.red(),
            ),
            DeploymentEventType.CRASHED: (
                "\U0001f6a8 Servicio detenido",
                messages.crashed_description,
                discord.Color.dark_red(),
            ),
            DeploymentEventType.OOM_KILLED: (
                "\U0001f9e0 Servicio sin memoria",
                messages.oom_description,
                discord.Color.orange(),
            ),
        }[event.event_type]
        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
            timestamp=event.timestamp,
        )

    deployment_lines = []
    if event.branch:
        deployment_lines.append(f"**Rama:** `{event.branch}`")
    if event.commit_hash:
        short_commit = event.commit_hash[:8]
        if (
            (event.source or "").casefold() == "github"
            and re.fullmatch(r"[0-9a-fA-F]{7,64}", event.commit_hash)
        ):
            commit_url = f"{manifest.repository_url}/commit/{event.commit_hash}"
            deployment_lines.append(f"**Commit:** [{short_commit}]({commit_url})")
        else:
            deployment_lines.append(f"**Commit:** `{short_commit}`")
    deployment_lines.append(f"**Hora:** {discord.utils.format_dt(event.timestamp, style='R')}")
    embed.add_field(name="Despliegue", value="\n".join(deployment_lines), inline=False)
    return embed
