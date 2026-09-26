"""Periodic, guild-scoped pending-report summaries with grouped reminders."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from uuid import UUID

import discord

from arrest_report_agent.application.member_names import person_name_aliases, person_name_match_strength
from arrest_report_agent.application.ports import GuildMemberProfile, GuildMemberSearchQuery
from arrest_report_agent.config import PendingArrestReportsOptions
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.discord.member_directory import DiscordGuildMemberDirectory
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider, GuildRuntimeBundle

from .digest_repository import PendingArrestDigestRepository
from .discord import PendingReportPaginationView
from .models import (
    ArrestReviewStatus,
    PendingArrestReport,
    PendingArrestReportScan,
    PendingReportScanTooLargeError,
)
from .service import (
    PCU_DEADLINE_HOURS,
    PCU_NEAR_DEADLINE_HOURS,
    REVIEW_DEADLINE_HOURS,
    REVIEW_NEAR_DEADLINE_HOURS,
    PendingArrestReportService,
)

LOGGER = logging.getLogger(__name__)
CHECK_SECONDS = 60
MEMBER_PAGE_SIZE = 100
MAX_MEMBER_PAGES = 50


def reminder_stage(report: PendingArrestReport) -> int:
    """2 is overdue, 1 is approaching a deadline, 0 is pending but on time."""

    if (report.pcu_reason is not None and report.age_hours >= PCU_DEADLINE_HOURS) or (
        report.review_status is not ArrestReviewStatus.APPROVED and report.age_hours >= REVIEW_DEADLINE_HOURS
    ):
        return 2
    if (report.pcu_reason is not None and report.age_hours >= PCU_NEAR_DEADLINE_HOURS) or (
        report.review_status is not ArrestReviewStatus.APPROVED
        and report.age_hours >= REVIEW_NEAR_DEADLINE_HOURS
    ):
        return 1
    return 0


def unique_member_match(
    officer_name: str, members: tuple[GuildMemberProfile, ...]
) -> tuple[GuildMemberProfile | None, bool]:
    ranked = [
        (
            person_name_match_strength(
                officer_name, person_name_aliases(member.display_name, member.username)
            ),
            member,
        )
        for member in members
    ]
    strongest = max((strength for strength, _ in ranked), default=0)
    if strongest == 0:
        return None, False
    matches = [member for strength, member in ranked if strength == strongest]
    return (matches[0], False) if len(matches) == 1 else (None, True)


def due_mentions(
    reports: tuple[PendingArrestReport, ...],
    matches: dict[int, int],
) -> tuple[tuple[int, int, int], ...]:
    """Mention every currently pending report with a resolved member each cycle."""

    return tuple(
        (report.report_id, matches[report.report_id], reminder_stage(report))
        for report in reports
        if report.report_id in matches
    )


def grouped_reply_chunks(guild: discord.Guild, mentions: tuple[tuple[int, int, int], ...]) -> tuple[str, ...]:
    """Group current debtors by urgency; split only when Discord's message cap requires it."""

    by_user: dict[int, int] = {}
    for _, user_id, stage in mentions:
        by_user[user_id] = max(stage, by_user.get(user_id, 0))
    groups = (
        (2, "parca", "💀", "Fuera de plazo"),
        (1, "conodelaverguenza", "🚧", "Próximos a vencer"),
        (0, "", "📝", "Pendientes"),
    )
    lines: list[str] = []
    for stage, emoji_name, fallback, title in groups:
        user_ids = sorted(user_id for user_id, level in by_user.items() if level == stage)
        if not user_ids:
            continue
        emoji = discord.utils.get(guild.emojis, name=emoji_name) if emoji_name else None
        prefix = f"{emoji or fallback} **{title}:**"
        line = prefix
        for user_id in user_ids:
            mention = f" <@{user_id}>"
            if len(line) + len(mention) > 1700:
                lines.append(line)
                line = prefix + mention
            else:
                line += mention
        lines.append(line)
    if not lines:
        return ()
    lines.append("Revisen sus reportes y pónganlos al día, po.")
    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = f"{current}\n\n{line}" if current else line
        if len(candidate) > 1900 and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return tuple(chunks)


def overdue_reasons(report: PendingArrestReport) -> tuple[str, ...]:
    """Describe only the PCU and review deadlines that have actually expired."""

    reasons: list[str] = []
    if report.pcu_reason is not None and report.age_hours >= PCU_DEADLINE_HOURS:
        hours = report.age_hours - PCU_DEADLINE_HOURS
        reasons.append(f"Sin PCU ({hours} h fuera de plazo)" if hours else "Sin PCU (plazo vencido)")
    if report.review_status is not ArrestReviewStatus.APPROVED and report.age_hours >= REVIEW_DEADLINE_HOURS:
        hours = report.age_hours - REVIEW_DEADLINE_HOURS
        label = "Sin revisión" if report.review_status is ArrestReviewStatus.MISSING else "Revisión pendiente"
        reasons.append(f"{label} ({hours} h fuera de plazo)" if hours else f"{label} (plazo vencido)")
    return tuple(reasons)


def build_supervisor_alert_embed(
    reports: tuple[PendingArrestReport, ...],
    matches: dict[int, int],
    *,
    summary_url: str,
) -> discord.Embed:
    """Summarize expired cases, with a direct link to the full command digest."""

    overdue = tuple(report for report in reports if overdue_reasons(report))
    by_officer: dict[int | str, tuple[str, list[str]]] = {}
    for report in overdue:
        user_id = matches.get(report.report_id)
        key = user_id if user_id is not None else " ".join(report.officer_name.split()).casefold()
        label = (
            f"<@{user_id}>"
            if user_id is not None
            else discord.utils.escape_markdown(discord.utils.escape_mentions(report.officer_name))
        )
        if key not in by_officer:
            by_officer[key] = (label, [])
        by_officer[key][1].append(f"• **#{report.report_id}** · {' · '.join(overdue_reasons(report))}")
    header = f"**{len(overdue)} reportes vencidos · {len(by_officer)} agentes**"
    link = f"🔎 [Ver el resumen completo en comandos]({summary_url})"
    sections: list[str] = []
    shown = 0
    for label, report_lines in by_officer.values():
        section_lines = [f"👮 **{label}**"]
        for line in report_lines:
            candidate = "\n\n".join((*sections, "\n".join((*section_lines, line))))
            if len(header) + len(candidate) + len(link) + 8 > 3900:
                break
            section_lines.append(line)
            shown += 1
        if len(section_lines) > 1:
            sections.append("\n".join(section_lines))
        if len(section_lines) - 1 < len(report_lines):
            break
    remaining = len(overdue) - shown
    if remaining:
        sections.append(f"…y {remaining} reportes más en el resumen completo.")
    details = "\n\n".join(sections)
    embed = discord.Embed(
        title="🚨 Reportes de arresto fuera de plazo",
        description=f"{header}\n\n{details}\n\n{link}",
        color=discord.Color.red(),
    )
    return embed


class PendingArrestDigestWorker:
    def __init__(
        self,
        bot: discord.Bot,
        provider: DatabaseConfigProvider,
        repository: PendingArrestDigestRepository,
        service: PendingArrestReportService,
        member_directory: DiscordGuildMemberDirectory,
        *,
        enabled_guild_ids: tuple[int, ...] = (),
        initial_options: dict[int, PendingArrestReportsOptions] | None = None,
    ) -> None:
        self.bot = bot
        self.provider = provider
        self.repository = repository
        self.service = service
        self.member_directory = member_directory
        self._enabled_guild_ids = set(enabled_guild_ids)
        self._interval_by_guild = {
            guild_id: options.digest_interval_hours for guild_id, options in (initial_options or {}).items()
        }
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._processing = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running or not self._enabled_guild_ids:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="pending-arrest-digest")

    async def close(self) -> None:
        self._stop.set()
        self._wake.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def configuration_updated(self, bundle: GuildRuntimeBundle) -> None:
        guild_id = bundle.snapshot.guild_id
        if self._enabled(bundle):
            options = bundle.settings.pending_arrest_reports_options
            assert options is not None
            previous = self._interval_by_guild.get(guild_id)
            if previous is not None and previous != options.digest_interval_hours:
                await self.repository.reschedule(
                    guild_id, now=datetime.now(UTC), interval_hours=options.digest_interval_hours
                )
            self._interval_by_guild[guild_id] = options.digest_interval_hours
            self._enabled_guild_ids.add(guild_id)
            self.start()
            self._wake.set()
        else:
            self._interval_by_guild.pop(guild_id, None)
            self._enabled_guild_ids.discard(guild_id)
            if not self._enabled_guild_ids:
                await self.close()

    @staticmethod
    def _enabled(bundle: GuildRuntimeBundle) -> bool:
        options = bundle.settings.pending_arrest_reports_options
        return bool(options is not None and options.digest_targets)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.process_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log_event(
                    LOGGER, "error", "pending_digest_poll_failed", exc_info=error, **exception_fields(error)
                )
            self._wake.clear()
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=CHECK_SECONDS)

    async def process_once(self, *, now: datetime | None = None) -> None:
        if not self.bot.is_ready():
            return
        current = now or datetime.now(UTC)
        async with self._processing:
            for guild_id in sorted(self._enabled_guild_ids):
                try:
                    await self._process_guild(guild_id, now=current)
                except Exception as error:
                    log_event(
                        LOGGER,
                        "error",
                        "pending_digest_guild_failed",
                        guild_id=guild_id,
                        exc_info=error,
                        **exception_fields(error),
                    )

    async def _process_guild(self, guild_id: int, *, now: datetime) -> None:
        bundle = await self.provider.cached(guild_id)
        if not self._enabled(bundle):
            return
        options = bundle.settings.pending_arrest_reports_options
        assert options is not None
        cycle_id = await self.repository.claim(
            guild_id, now=now, interval_hours=options.digest_interval_hours
        )
        if cycle_id is None:
            return
        try:
            result = await self.service.scan(
                faction_id=options.faction_id,
                date_from=now.date() - timedelta(days=options.digest_lookback_days - 1),
                date_to=now.date(),
                max_records=options.max_records,
                now=now,
                guild_id=guild_id,
                report_channel_ids=bundle.settings.report_channel_allowlist.ids,
                authorized_reviewer_role_ids=bundle.settings.supervisor_role_allowlist.ids,
            )
            if result.pending_count:
                await self._publish(bundle, options, result, cycle_id=cycle_id, now=now)
            await self.repository.finish(
                guild_id, cycle_id, now=datetime.now(UTC), interval_hours=options.digest_interval_hours
            )
            log_event(
                LOGGER,
                "info",
                "pending_digest_completed",
                guild_id=guild_id,
                pending_reports=result.pending_count,
                destination_count=len(options.digest_targets),
            )
        except PendingReportScanTooLargeError as error:
            await self.repository.finish(
                guild_id, cycle_id, now=datetime.now(UTC), interval_hours=options.digest_interval_hours
            )
            log_event(
                LOGGER,
                "warning",
                "pending_digest_scan_limit_exceeded",
                guild_id=guild_id,
                total_reports=error.total,
                max_records=error.limit,
            )
        except asyncio.CancelledError:
            await self.repository.retry(guild_id, cycle_id, now=datetime.now(UTC))
            raise
        except Exception:
            await self.repository.retry(guild_id, cycle_id, now=datetime.now(UTC))
            raise

    async def _publish(
        self,
        bundle: GuildRuntimeBundle,
        options: PendingArrestReportsOptions,
        result: PendingArrestReportScan,
        *,
        cycle_id: UUID,
        now: datetime,
    ) -> None:
        guild_id = bundle.snapshot.guild_id
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            raise RuntimeError("Pending digest guild is unavailable")
        deliveries = await self.repository.deliveries(guild_id, cycle_id)
        mention_target = next((target for target in options.digest_targets if target.mention_users), None)
        overdue = tuple(report for report in result.pending_reports if overdue_reasons(report))
        matches: dict[int, int] = {}
        mentions: tuple[tuple[int, int, int], ...] = ()
        mention_reply_due = mention_target is not None and not deliveries.get(
            mention_target.channel_id, (0, False)
        )[1]
        if mention_reply_due or (options.supervisor_alerts_enabled and overdue):
            try:
                members = await self._members(guild_id, bundle.settings.operational_staff_role_ids)
            except Exception as error:
                members = ()
                log_event(
                    LOGGER,
                    "warning",
                    "pending_digest_member_lookup_failed",
                    guild_id=guild_id,
                    exc_info=error,
                    **exception_fields(error),
                )
            ambiguous = 0
            for report in result.pending_reports:
                profile, is_ambiguous = unique_member_match(report.officer_name, members)
                ambiguous += int(is_ambiguous)
                if profile is not None:
                    matches[report.report_id] = profile.user_id
            if mention_reply_due:
                mentions = due_mentions(result.pending_reports, matches)
            if ambiguous:
                log_event(
                    LOGGER,
                    "warning",
                    "pending_digest_member_match_ambiguous",
                    guild_id=guild_id,
                    ambiguous_reports=ambiguous,
                )

        ui = bundle.persona.profile.messages.interface.pending_arrest_reports
        for target in options.digest_targets:
            try:
                channel = await self._channel(guild, target.channel_id)
                existing = deliveries.get(target.channel_id)
                if existing is None:
                    view = PendingReportPaginationView(result=result, ui=ui)
                    if view.page_count > 1:
                        message = await channel.send(
                            embed=view.current_embed,
                            view=view,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    else:
                        message = await channel.send(
                            embed=view.current_embed,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    await self.repository.record_delivery(guild_id, cycle_id, target.channel_id, message.id)
                    deliveries[target.channel_id] = (message.id, False)
                elif target.mention_users and not existing[1] and mentions:
                    message = await channel.fetch_message(existing[0])
                else:
                    continue
                if target.mention_users and mentions:
                    allowed = discord.AllowedMentions(
                        everyone=False,
                        roles=False,
                        users=[
                            discord.Object(id=user_id) for user_id in sorted({item[1] for item in mentions})
                        ],
                        replied_user=False,
                    )
                    for content in grouped_reply_chunks(guild, mentions):
                        await message.reply(content, mention_author=False, allowed_mentions=allowed)
                    await self.repository.record_mentions(guild_id, mentions, now=now)
                if target.mention_users:
                    await self.repository.record_reply(guild_id, cycle_id, target.channel_id)
                    deliveries[target.channel_id] = (message.id, True)
            except Exception as error:
                log_event(
                    LOGGER,
                    "error",
                    "pending_digest_delivery_failed",
                    guild_id=guild_id,
                    channel_id=target.channel_id,
                    exc_info=error,
                    **exception_fields(error),
                )
                raise

        if not options.supervisor_alerts_enabled or not overdue:
            return
        if mention_target is None or options.supervisor_alert_role_id is None:
            raise ValueError("Supervisor alert requires a mentioning digest target and role")
        source = deliveries.get(mention_target.channel_id)
        if source is None:
            raise RuntimeError("Supervisor alert requires a published command digest")
        role = guild.get_role(options.supervisor_alert_role_id)
        if role is None:
            raise ValueError("Supervisor alert role is unavailable")
        summary_url = (
            f"https://discord.com/channels/{guild_id}/{mention_target.channel_id}/{source[0]}"
        )
        embed = build_supervisor_alert_embed(overdue, matches, summary_url=summary_url)
        allowed = discord.AllowedMentions(
            everyone=False,
            roles=[discord.Object(id=role.id)],
            users=False,
            replied_user=False,
        )
        for channel_id in options.supervisor_alert_channel_ids:
            if channel_id in deliveries:
                continue
            try:
                channel = await self._channel(guild, channel_id)
                if not role.mentionable and not channel.permissions_for(guild.me).mention_everyone:
                    raise ValueError("Bot cannot mention the supervisor alert role in this channel")
                message = await channel.send(
                    content=role.mention,
                    embed=embed,
                    allowed_mentions=allowed,
                )
                await self.repository.record_delivery(guild_id, cycle_id, channel_id, message.id)
                deliveries[channel_id] = (message.id, False)
            except Exception as error:
                log_event(
                    LOGGER,
                    "error",
                    "pending_digest_supervisor_delivery_failed",
                    guild_id=guild_id,
                    channel_id=channel_id,
                    exc_info=error,
                    **exception_fields(error),
                )
                raise

    async def _members(self, guild_id: int, role_ids: tuple[int, ...]) -> tuple[GuildMemberProfile, ...]:
        if not role_ids:
            return ()
        members: list[GuildMemberProfile] = []
        cursor: str | None = None
        for _ in range(MAX_MEMBER_PAGES):
            result = await self.member_directory.search_members(
                guild_id,
                GuildMemberSearchQuery(
                    query=None,
                    user_ids=(),
                    role_ids_any=role_ids,
                    role_ids_all=(),
                    role_ids_none=(),
                    is_bot=False,
                    joined_after=None,
                    joined_before=None,
                    sort_by="display_name",
                    sort_direction="asc",
                    limit=MEMBER_PAGE_SIZE,
                    cursor=cursor,
                ),
            )
            if result.error is not None:
                raise RuntimeError(f"Discord member directory failed: {result.error}")
            members.extend(result.members)
            cursor = result.next_cursor
            if cursor is None:
                return tuple(members)
        raise RuntimeError("Discord member directory exceeded the pagination limit")

    async def _channel(self, guild: discord.Guild, channel_id: int) -> discord.TextChannel:
        channel = guild.get_channel(channel_id)
        resolved = channel if channel is not None else await guild.fetch_channel(channel_id)
        if not isinstance(resolved, discord.TextChannel) or resolved.guild.id != guild.id:
            raise ValueError("Pending digest destination must be a guild text channel")
        if guild.me is None:
            raise RuntimeError("Bot member is unavailable")
        permissions = resolved.permissions_for(guild.me)
        if not (
            permissions.view_channel
            and permissions.send_messages
            and permissions.embed_links
            and permissions.read_message_history
        ):
            raise ValueError("Missing pending digest channel permissions")
        return resolved
