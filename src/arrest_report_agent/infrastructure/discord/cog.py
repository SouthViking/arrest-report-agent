"""Discord event listener and deterministic slash commands."""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass, replace
from time import monotonic
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from arrest_report_agent.agent_sessions import (
    AgentConversationKey,
    AgentSessionManager,
    PreparedAgentSession,
)
from arrest_report_agent.application import (
    PersonalReportPeriod,
    PersonalReportQuery,
    PersonalReportStatusFilter,
    ReviewRunContext,
    ReviewService,
)
from arrest_report_agent.application.errors import public_error_message
from arrest_report_agent.config import (
    AppSettings,
    BotPersona,
    GuildRuntimeSettings,
    PersonaAccessMessages,
)
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.domain import AgentAccessLevel, ErrorKind, ReviewCase
from arrest_report_agent.domain.errors import ContextUnavailableError, ReportOwnerSubmissionError
from arrest_report_agent.report_owner_access import (
    OwnerRunReservationResult,
    ReportOwnerRunRepository,
)
from arrest_report_agent.review_analytics import RecordingReviewPublisher, ReportReviewRepository
from arrest_report_agent.review_analytics.discord import ReportErrorsModal
from arrest_report_agent.support import append_support_note

from .context import (
    REPORT_OWNER_MINIMUM_IMAGE_COUNT,
    DiscordContextBuilder,
    render_agent_input,
)
from .general_knowledge import (
    ChargeSearchModal,
    FaqNavigationView,
    GuideNavigationView,
)
from .member_directory import DiscordGuildMemberDirectory
from .message_directory import DiscordGuildMessageDirectory
from .personal_reports import (
    DiscordPersonalReportFinder,
    PersonalReportsModal,
    build_personal_summary_embed,
)
from .presence import ReviewPresence
from .profile import DiscordProfileSynchronizer
from .publisher import DiscordReviewPublisher
from .supervision import (
    HonorRankingModal,
    PendingReportsModal,
    RankingModal,
    ShameRankingModal,
    SupervisorRolePolicy,
    build_supervisor_unauthorized_message,
)
from .trigger import DiscordAccessPolicy, TriggerDecision

if TYPE_CHECKING:
    from arrest_report_agent.application.ports import PollManager
    from arrest_report_agent.manual_quiz import ManualQuizDiscordPublisher

LOGGER = logging.getLogger(__name__)

DETERMINISTIC_REPLY_COOLDOWN_SECONDS = 30.0
TEAMSPEAK_SERVER_ADDRESS = "51.81.49.72:11189"


@dataclass(frozen=True, slots=True)
class ReviewCogRuntime:
    """All guild-scoped collaborators captured for one Discord interaction."""

    settings: AppSettings | GuildRuntimeSettings
    service: ReviewService
    persona: BotPersona
    access_policy: DiscordAccessPolicy
    supervisor_policy: SupervisorRolePolicy
    report_analytics_policy: SupervisorRolePolicy
    context_builder: DiscordContextBuilder
    personal_report_finder: DiscordPersonalReportFinder


@asynccontextmanager
async def maintain_typing_indicator(
    channel: discord.TextChannel | discord.Thread,
) -> AsyncIterator[None]:
    """Keep Discord's native typing indicator active without blocking the review on failure."""

    stack = AsyncExitStack()
    try:
        await stack.enter_async_context(channel.typing())
    except (discord.Forbidden, discord.HTTPException) as error:
        log_event(
            LOGGER,
            "warning",
            "typing_indicator_unavailable",
            exc_info=error,
            channel_id=channel.id,
            **exception_fields(error),
        )
    try:
        yield
    finally:
        with suppress(discord.Forbidden, discord.HTTPException):
            await stack.aclose()


def build_help_embed(
    *,
    persona: BotPersona,
    avatar_url: str | None = None,
    extra_feature_guides: str | None = None,
) -> discord.Embed:
    """Render a compact directory for the feature-specific help commands."""

    identity = persona.identity
    copy = persona.messages.help
    embed = discord.Embed(
        title=copy.title.format(
            display_name=identity.display_name,
            role_title=identity.role_title,
        ),
        description=copy.description.format(
            display_name=identity.display_name,
            role_title=identity.role_title,
        ),
        color=discord.Color.gold(),
    )
    embed.add_field(
        name=copy.feature_guides_title,
        value=(
            f"{copy.feature_guides}\n{extra_feature_guides}" if extra_feature_guides else copy.feature_guides
        ),
        inline=False,
    )
    embed.add_field(
        name=copy.quick_references_title,
        value=copy.quick_references,
        inline=False,
    )
    embed.set_footer(text=copy.footer.format(form_of_address=identity.default_form_of_address))
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    return append_support_note(embed)


def build_report_info_embed(
    *,
    persona: BotPersona,
    bot_mention: str,
    report_channel_ids: frozenset[int] | None,
    report_owner_max_rounds: int | None = None,
    avatar_url: str | None = None,
) -> discord.Embed:
    """Explain the bounded self-review workflow for report authors."""

    copy = persona.messages.help
    channels = (
        "\n".join(f"• <#{channel_id}>" for channel_id in sorted(report_channel_ids))
        if report_channel_ids is not None
        else copy.unrestricted_channels
    )
    embed = discord.Embed(
        title="📋 Revisa tu propio reporte",
        description=(
            copy.owner_info_intro if report_owner_max_rounds is not None else copy.owner_info_unavailable
        ),
        color=discord.Color.gold(),
    )
    if report_owner_max_rounds is not None:
        embed.add_field(
            name="1️⃣ Cómo empezar",
            value=copy.owner_info_start.format(bot_mention=bot_mention),
            inline=False,
        )
        embed.add_field(
            name="2️⃣ Material mínimo",
            value=copy.owner_info_requirements,
            inline=False,
        )
        embed.add_field(
            name="3️⃣ Cómo continuar",
            value=copy.owner_info_continue,
            inline=False,
        )
        embed.add_field(
            name="🕒 Límite",
            value=copy.owner_info_limit.format(max_rounds=report_owner_max_rounds),
            inline=False,
        )
        embed.add_field(name="📍 Canales habilitados", value=channels, inline=False)
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    return embed


def build_teamspeak_embed(
    *,
    persona: BotPersona,
    address: str = TEAMSPEAK_SERVER_ADDRESS,
) -> discord.Embed:
    """Build the public, persona-aware TeamSpeak connection notice."""

    copy = persona.messages.interface.commands
    return discord.Embed(
        title="🎙️ TeamSpeak 3",
        description=copy.teamspeak_message.format(address=address),
        color=discord.Color.blurple(),
    )


def build_manual_embed(
    *,
    organization_name: str,
    organization_acronym: str,
    manual_url: str,
) -> discord.Embed:
    """Build the public shortcut to the configured faction manual."""

    embed = discord.Embed(
        title=f"📘 Manual de procedimientos {organization_acronym}",
        description=(
            "Consulta aquí el manual oficial de la facción.\n\n"
            f"🔗 [Abrir manual de {organization_acronym}]({manual_url})"
        ),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text=organization_name)
    return embed


def build_unauthorized_user_message(
    *,
    messages: PersonaAccessMessages,
    message_id: int,
    form_of_address: str,
) -> str:
    """Build a concise in-character access notice without invoking AI."""

    intro = messages.unauthorized_user_intros[message_id % len(messages.unauthorized_user_intros)]
    return messages.unauthorized_user.format(
        form_of_address=form_of_address,
        intro=intro,
    )


def build_blocked_user_message(
    *,
    messages: PersonaAccessMessages,
    message_id: int,
) -> str:
    """Build a distinct in-character notice for an explicitly blocked user."""

    return messages.blocked_user_messages[message_id % len(messages.blocked_user_messages)]


def build_owner_quota_exhausted_message(
    *,
    messages: PersonaAccessMessages,
    message_id: int,
    max_rounds: int,
) -> str:
    """Select the dedicated owner-quota notice without invoking AI."""

    pool = messages.owner_quota_exhausted_messages
    return pool[message_id % len(pool)].format(max_rounds=max_rounds)


def build_owner_invalid_report_message(
    *,
    messages: PersonaAccessMessages,
    message_id: int,
    bot_mention: str,
) -> str:
    """Explain the owner submission contract without invoking AI or spending a round."""

    pool = messages.owner_invalid_report_messages
    return pool[message_id % len(pool)].format(
        bot_mention=bot_mention,
        min_images=REPORT_OWNER_MINIMUM_IMAGE_COUNT,
    )


class ReviewCog(commands.Cog):
    def __init__(
        self,
        bot: discord.Bot,
        settings: AppSettings,
        service: ReviewService,
        persona: BotPersona,
        profile_synchronizer: DiscordProfileSynchronizer | None = None,
        config_provider: object | None = None,
        report_review_repository: ReportReviewRepository | None = None,
        report_owner_run_repository: ReportOwnerRunRepository | None = None,
        manual_quiz_publisher: ManualQuizDiscordPublisher | None = None,
        poll_manager: PollManager | None = None,
        agent_session_manager: AgentSessionManager | None = None,
    ) -> None:
        self.bot = bot
        self._config_provider = config_provider
        self.report_review_repository = report_review_repository
        self.report_owner_run_repository = report_owner_run_repository
        self.manual_quiz_publisher = manual_quiz_publisher
        self.poll_manager = poll_manager
        self.agent_session_manager = agent_session_manager
        self._runtime_context: contextvars.ContextVar[ReviewCogRuntime | None] = contextvars.ContextVar(
            "review_cog_runtime", default=None
        )
        self._runtime_cache: dict[object, ReviewCogRuntime] = {}
        self._default_runtime = self._build_runtime(settings, service, persona)
        command_copy = persona.messages.interface.commands
        self.review_help.description = command_copy.help_description
        self.report_info.description = command_copy.info_reports_description
        self.report_guide.description = command_copy.guide_description
        self.charge_search.description = command_copy.charges_description
        self.report_faq.description = command_copy.faq_description
        self.supervisor_pending.description = command_copy.pending_description
        self.supervisor_ranking.description = command_copy.ranking_description
        self.open_report_shame.description = command_copy.shame_description
        self.approved_report_honor.description = command_copy.honor_description
        self.report_errors.description = command_copy.report_errors_description
        self.personal_reports.description = command_copy.personal_reports_description
        self.personal_summary.description = command_copy.personal_summary_description
        self.teamspeak_server.description = command_copy.teamspeak_description
        self.profile_synchronizer = profile_synchronizer
        self.member_directory = DiscordGuildMemberDirectory(bot)
        self.message_directory = DiscordGuildMessageDirectory(bot)
        self.presence = ReviewPresence(
            bot,
            idle_text=persona.presence.idle,
            conversation_text=persona.presence.conversation,
            knowledge_text=persona.presence.knowledge,
            manual_index_text=persona.presence.manual_index,
            manual_search_text=persona.presence.manual_search,
            manual_read_text=persona.presence.manual_read,
            frequencies_text=persona.presence.frequencies,
            personnel_text=persona.presence.personnel,
            records_text=persona.presence.records,
            evidence_text=persona.presence.evidence,
            evidence_bag_text=persona.presence.evidence_bag,
            review_text=persona.presence.review,
            response_text=persona.presence.response,
        )
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_reviews)
        self._locks: dict[int, asyncio.Lock] = {}
        self._attempts: defaultdict[int, int] = defaultdict(int)
        self._deterministic_reply_cooldowns: dict[tuple[int, int, int, str], float] = {}
        self._unavailable_report_channel_keys: set[tuple[int, int]] = set()

    @property
    def _runtime(self) -> ReviewCogRuntime:
        return self._runtime_context.get() or self._default_runtime

    @property
    def settings(self) -> AppSettings | GuildRuntimeSettings:
        return self._runtime.settings

    @property
    def service(self) -> ReviewService:
        return self._runtime.service

    @property
    def persona(self) -> BotPersona:
        return self._runtime.persona

    @property
    def access_policy(self) -> DiscordAccessPolicy:
        return self._runtime.access_policy

    @property
    def supervisor_policy(self) -> SupervisorRolePolicy:
        return self._runtime.supervisor_policy

    @property
    def report_analytics_policy(self) -> SupervisorRolePolicy:
        return self._runtime.report_analytics_policy

    @property
    def context_builder(self) -> DiscordContextBuilder:
        return self._runtime.context_builder

    @context_builder.setter
    def context_builder(self, value: DiscordContextBuilder) -> None:
        self._default_runtime = replace(self._default_runtime, context_builder=value)

    @property
    def personal_report_finder(self) -> DiscordPersonalReportFinder:
        return self._runtime.personal_report_finder

    @personal_report_finder.setter
    def personal_report_finder(self, value: DiscordPersonalReportFinder) -> None:
        self._default_runtime = replace(self._default_runtime, personal_report_finder=value)

    def _build_runtime(
        self,
        settings: AppSettings | GuildRuntimeSettings,
        service: ReviewService,
        persona: BotPersona,
    ) -> ReviewCogRuntime:
        return ReviewCogRuntime(
            settings=settings,
            service=service,
            persona=persona,
            access_policy=DiscordAccessPolicy.from_settings(settings),  # type: ignore[arg-type]
            supervisor_policy=SupervisorRolePolicy.from_settings(settings),  # type: ignore[arg-type]
            report_analytics_policy=SupervisorRolePolicy(
                roles=getattr(
                    settings,
                    "report_analytics_role_allowlist",
                    settings.supervisor_role_allowlist,
                )
            ),
            context_builder=DiscordContextBuilder(settings, persona, self.bot),  # type: ignore[arg-type]
            personal_report_finder=DiscordPersonalReportFinder(self.bot, settings),  # type: ignore[arg-type]
        )

    async def _resolve_runtime(self, guild_id: int) -> ReviewCogRuntime:
        if self._config_provider is None:
            return self._default_runtime
        bundle = await self._config_provider.cached(guild_id)  # type: ignore[attr-defined]
        cache_key = bundle.snapshot.revision_id
        runtime = self._runtime_cache.get(cache_key)
        if runtime is None:
            runtime = self._build_runtime(
                bundle.settings,
                bundle.service,
                bundle.persona.profile,
            )
            self._runtime_cache[cache_key] = runtime
        return runtime

    @asynccontextmanager
    async def _bind_guild_runtime(self, guild_id: int) -> AsyncIterator[None]:
        token = self._runtime_context.set(await self._resolve_runtime(guild_id))
        try:
            yield
        finally:
            self._runtime_context.reset(token)

    async def cog_before_invoke(self, ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None:
            return
        token = self._runtime_context.set(await self._resolve_runtime(ctx.guild.id))
        ctx._database_runtime_token = token  # type: ignore[attr-defined]

    async def cog_after_invoke(self, ctx: discord.ApplicationContext) -> None:
        token = getattr(ctx, "_database_runtime_token", None)
        if token is not None:
            self._runtime_context.reset(token)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self.profile_synchronizer is not None:
            await self.profile_synchronizer.synchronize()
        await self.presence.synchronize()
        log_event(
            LOGGER,
            "info",
            "discord_ready",
            bot_user_id=self.bot.user.id if self.bot.user else "unknown",
            guild_access_mode=("restricted" if self.access_policy.guilds.is_restricted else "open"),
            configured_guilds=self.access_policy.guilds.configured_count,
            report_channel_mode=(
                "restricted" if self.settings.report_channel_allowlist.is_restricted else "open"
            ),
            configured_report_channels=self.settings.report_channel_allowlist.configured_count,
            user_access_mode=("restricted" if self.access_policy.users.is_restricted else "open"),
            configured_users=self.access_policy.users.configured_count,
            configured_blocked_users=self.access_policy.blocked_users.configured_count,
            report_owner_access_enabled=(
                getattr(self.settings, "report_owner_access_max_rounds", None) is not None
            ),
            report_owner_max_rounds=(
                getattr(self.settings, "report_owner_access_max_rounds", None) or "none"
            ),
            supervisor_access_mode=("restricted" if self.supervisor_policy.is_restricted else "open"),
            configured_supervisor_roles=self.supervisor_policy.roles.configured_count,
            persona_id=self.persona.id,
            persona_revision=self.persona.revision,
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        try:
            async with self._bind_guild_runtime(message.guild.id):
                await self._handle_message(message)
        except LookupError:
            return

    async def _handle_message(self, message: discord.Message) -> None:
        bot_user = self.bot.user
        if bot_user is None:
            return
        evaluation = await self.access_policy.evaluate_event(message, bot_user.id)
        decision = evaluation.decision
        if decision is TriggerDecision.REDIRECT_BLOCKED_USER:
            await self._redirect_blocked_user(message)
            return
        access_level = AgentAccessLevel.FULL
        target = None
        if decision is TriggerDecision.REDIRECT_UNAUTHORIZED_USER:
            max_rounds = getattr(self.settings, "report_owner_access_max_rounds", None)
            if max_rounds is None or evaluation.activation is None:
                await self._redirect_unauthorized_user(message)
                return
            try:
                target = await self.context_builder.resolve_report_owner_target(
                    message,
                    evaluation.activation,
                    bot_user_id=bot_user.id,
                )
            except ReportOwnerSubmissionError:
                await self._redirect_owner_invalid_report(
                    message,
                    bot_mention=getattr(bot_user, "mention", f"<@{bot_user.id}>"),
                )
                return
            except ContextUnavailableError:
                await self._redirect_unauthorized_user(message)
                return
            access_level = AgentAccessLevel.REPORT_OWNER
        if decision is not TriggerDecision.REVIEW and access_level is not AgentAccessLevel.REPORT_OWNER:
            return
        if evaluation.activation is None:
            log_event(
                LOGGER,
                "warning",
                "review_activation_unavailable",
                message_id=message.id,
                channel_id=message.channel.id,
                user_id=message.author.id,
            )
            return
        if target is None:
            try:
                target = await self.context_builder.resolve_target(
                    message,
                    evaluation.activation,
                )
            except ContextUnavailableError as error:
                log_event(
                    LOGGER,
                    "warning",
                    "review_target_unavailable",
                    exc_info=error,
                    message_id=message.id,
                    channel_id=message.channel.id,
                    user_id=message.author.id,
                    **exception_fields(error),
                )
                return
        root_id = target.root.id
        lock = self._locks.setdefault(root_id, asyncio.Lock())
        if lock.locked():
            log_event(
                LOGGER,
                "info",
                "duplicate_review_ignored",
                root_message_id=root_id,
                trigger_message_id=message.id,
            )
            return
        try:
            async with (
                lock,
                maintain_typing_indicator(message.channel),
                self._semaphore,
                self.presence.handling() as activity,
            ):
                self._attempts[root_id] += 1
                attempt = self._attempts[root_id]
                publisher = DiscordReviewPublisher(
                    self.bot,
                    self.settings,
                    target,
                    self.persona,
                    allow_outgoing_mentions=access_level is AgentAccessLevel.FULL,
                    manual_quiz_publisher=self.manual_quiz_publisher,
                )
                try:
                    case = (
                        await self.context_builder.build_case(
                            target,
                            include_channel_history=False,
                        )
                        if access_level is AgentAccessLevel.REPORT_OWNER
                        else await self.context_builder.build_case(target)
                    )
                except Exception as error:
                    log_event(
                        LOGGER,
                        "error",
                        "context_build_failed",
                        exc_info=error,
                        root_message_id=root_id,
                        trigger_message_id=message.id,
                        technical_reference=f"{root_id}-{attempt}-context_unavailable",
                        **exception_fields(error),
                    )
                    try:
                        await publisher.publish_failure(
                            public_message=public_error_message(
                                ErrorKind.CONTEXT_UNAVAILABLE,
                                self.persona.messages.failures,
                            ),
                        )
                    except Exception as publication_error:
                        log_event(
                            LOGGER,
                            "error",
                            "context_failure_publication_failed",
                            exc_info=publication_error,
                            root_message_id=root_id,
                            technical_reference=f"{root_id}-{attempt}-context_unavailable",
                            **exception_fields(publication_error),
                        )
                    return
                owner_reservation = None
                if access_level is AgentAccessLevel.REPORT_OWNER:
                    max_rounds = getattr(self.settings, "report_owner_access_max_rounds", None)
                    if max_rounds is None or self.report_owner_run_repository is None:
                        await self._redirect_unauthorized_user(message)
                        return
                    try:
                        owner_reservation = await self.report_owner_run_repository.reserve(
                            guild_id=case.guild_id,
                            root_message_id=case.root_message_id,
                            trigger_message_id=case.trigger_message_id,
                            owner_user_id=case.requester_user_id,
                            thread_id=case.thread_id,
                            max_rounds=max_rounds,
                        )
                    except Exception as error:
                        log_event(
                            LOGGER,
                            "error",
                            "report_owner_run_reservation_failed",
                            exc_info=error,
                            guild_id=case.guild_id,
                            root_message_id=case.root_message_id,
                            trigger_message_id=case.trigger_message_id,
                            user_id=case.requester_user_id,
                            access_level=access_level.value,
                            **exception_fields(error),
                        )
                        await self._redirect_unauthorized_user(message)
                        return
                    log_event(
                        LOGGER,
                        "info",
                        "report_owner_run_reservation",
                        guild_id=case.guild_id,
                        parent_channel_id=case.parent_channel_id,
                        thread_id=case.thread_id or "none",
                        root_message_id=case.root_message_id,
                        trigger_message_id=case.trigger_message_id,
                        user_id=case.requester_user_id,
                        access_level=access_level.value,
                        reservation_result=owner_reservation.result.value,
                        used_rounds=owner_reservation.used_rounds,
                        max_rounds=owner_reservation.max_rounds,
                    )
                    if owner_reservation.result is OwnerRunReservationResult.DUPLICATE:
                        return
                    if owner_reservation.result is OwnerRunReservationResult.EXHAUSTED:
                        await self._redirect_owner_quota_exhausted(
                            message,
                            max_rounds=owner_reservation.max_rounds,
                        )
                        return
                run_context = ReviewRunContext(
                    case=case,
                    publisher=(
                        RecordingReviewPublisher(
                            publisher,
                            self.report_review_repository,
                            case,
                            model=self.service.model.id,
                            reasoning_effort=self.service.model.reasoning_effort,
                            persona_id=self.persona.id,
                            persona_revision=self.persona.revision,
                            app_version=self.settings.app_version,
                        )
                        if self.report_review_repository is not None
                        else publisher
                    ),
                    attempt=attempt,
                    access_level=access_level,
                    owner_round=(owner_reservation.round_number if owner_reservation is not None else None),
                    owner_max_rounds=(
                        owner_reservation.max_rounds if owner_reservation is not None else None
                    ),
                    owner_reservation_result=(
                        owner_reservation.result.value if owner_reservation is not None else None
                    ),
                    activity=activity,
                    member_directory=self.member_directory,
                    message_directory=self.message_directory,
                    poll_manager=self.poll_manager,
                )
                prepared_session = await self._prepare_agent_session(case, access_level)
                source_cursor = self._source_cursor_as_message_id(prepared_session)
                if (
                    prepared_session is not None
                    and prepared_session.source_cursor is not None
                    and source_cursor is None
                ):
                    prepared_session = None
                agent_input = render_agent_input(case, after_message_id=source_cursor)
                try:
                    if prepared_session is None:
                        await self.service.review(run_context, agent_input)
                    else:
                        await self.service.review(
                            run_context,
                            agent_input,
                            session=prepared_session.session,
                            stateless_agent_input=render_agent_input(case),
                        )
                finally:
                    if owner_reservation is not None and owner_reservation.run_id is not None:
                        try:
                            if run_context.published or run_context.interaction_completed:
                                await self.report_owner_run_repository.mark_completed(
                                    owner_reservation.run_id
                                )
                            else:
                                await self.report_owner_run_repository.mark_failed(owner_reservation.run_id)
                        except Exception as error:
                            log_event(
                                LOGGER,
                                "error",
                                "report_owner_run_finalization_failed",
                                exc_info=error,
                                guild_id=case.guild_id,
                                root_message_id=case.root_message_id,
                                trigger_message_id=case.trigger_message_id,
                                user_id=case.requester_user_id,
                                access_level=access_level.value,
                                owner_round=owner_reservation.round_number,
                                max_rounds=owner_reservation.max_rounds,
                                **exception_fields(error),
                            )
        finally:
            if not lock.locked():
                self._locks.pop(root_id, None)

    async def _prepare_agent_session(
        self,
        case: ReviewCase,
        access_level: AgentAccessLevel,
    ) -> PreparedAgentSession | None:
        """Activate generic persistence only for configured report conversations."""

        manager = self.agent_session_manager
        if manager is None:
            return None
        configured_channels = self.settings.report_channel_allowlist.ids
        if (
            configured_channels is None
            or case.parent_channel_id not in configured_channels
        ):
            return None
        key = AgentConversationKey(
            namespace="discord_agent_thread",
            scope_key=f"guild:{case.guild_id}:root:{case.root_message_id}",
            access_partition=access_level.value,
        )
        next_cursor = str(max((message.id for message in case.messages), default=case.trigger_message_id))
        try:
            return await manager.prepare(key, next_source_cursor=next_cursor)
        except Exception as error:
            log_event(
                LOGGER,
                "warning",
                "agent_session_prepare_failed",
                exc_info=error,
                guild_id=case.guild_id,
                parent_channel_id=case.parent_channel_id,
                thread_id=case.thread_id or "none",
                root_message_id=case.root_message_id,
                trigger_message_id=case.trigger_message_id,
                access_level=access_level.value,
                fallback="stateless_full_context",
                **exception_fields(error),
            )
            return None

    @staticmethod
    def _source_cursor_as_message_id(session: PreparedAgentSession | None) -> int | None:
        if session is None or session.source_cursor is None:
            return None
        try:
            return int(session.source_cursor)
        except ValueError:
            log_event(
                LOGGER,
                "warning",
                "agent_session_cursor_invalid",
                namespace="discord_agent_thread",
                fallback="full_context",
            )
            return None

    async def _resolve_report_channels(
        self,
        *,
        guild: discord.Guild | None,
        requester: discord.Member | discord.User,
        channel_ids: frozenset[int],
    ) -> tuple[int, ...]:
        """Resolve report channels visible in the requester's current guild."""

        if guild is None:
            return ()

        visible_ids: list[int] = []
        for channel_id in sorted(channel_ids):
            cache_key = (guild.id, channel_id)
            if cache_key in self._unavailable_report_channel_keys:
                continue
            channel = guild.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await guild.fetch_channel(channel_id)
                except discord.NotFound as error:
                    self._unavailable_report_channel_keys.add(cache_key)
                    log_event(
                        LOGGER,
                        "warning",
                        "configured_report_channel_missing",
                        exc_info=error,
                        guild_id=guild.id,
                        channel_id=channel_id,
                        **exception_fields(error),
                    )
                    continue
                except discord.InvalidData:
                    # A flat list may contain a valid channel from another configured guild.
                    self._unavailable_report_channel_keys.add(cache_key)
                    log_event(
                        LOGGER,
                        "info",
                        "configured_report_channel_belongs_to_another_guild",
                        guild_id=guild.id,
                        channel_id=channel_id,
                    )
                    continue
                except (discord.Forbidden, discord.HTTPException) as error:
                    log_event(
                        LOGGER,
                        "warning",
                        "configured_report_channel_lookup_failed",
                        exc_info=error,
                        guild_id=guild.id,
                        channel_id=channel_id,
                        **exception_fields(error),
                    )
                    continue

            if not isinstance(channel, discord.TextChannel):
                log_event(
                    LOGGER,
                    "warning",
                    "configured_report_channel_unsupported",
                    guild_id=guild.id,
                    channel_id=channel_id,
                    channel_type=type(channel).__name__,
                )
                continue

            bot_member = guild.me
            if bot_member is None or not channel.permissions_for(bot_member).view_channel:
                log_event(
                    LOGGER,
                    "warning",
                    "configured_report_channel_inaccessible",
                    guild_id=guild.id,
                    channel_id=channel_id,
                )
                continue
            if not channel.permissions_for(requester).view_channel:
                continue
            visible_ids.append(channel_id)

        return tuple(visible_ids)

    async def _redirect_unauthorized_user(self, message: discord.Message) -> None:
        await self._reply_deterministic_notice(
            message,
            content=build_unauthorized_user_message(
                messages=self.persona.messages.access,
                message_id=message.id,
                form_of_address=self.persona.identity.default_form_of_address,
            ),
            event_prefix="unauthorized_user_redirect",
        )

    async def _redirect_blocked_user(self, message: discord.Message) -> None:
        await self._reply_deterministic_notice(
            message,
            content=build_blocked_user_message(
                messages=self.persona.messages.access,
                message_id=message.id,
            ),
            event_prefix="blocked_user_redirect",
        )

    async def _redirect_owner_quota_exhausted(
        self,
        message: discord.Message,
        *,
        max_rounds: int,
    ) -> None:
        await self._reply_deterministic_notice(
            message,
            content=build_owner_quota_exhausted_message(
                messages=self.persona.messages.access,
                message_id=message.id,
                max_rounds=max_rounds,
            ),
            event_prefix="report_owner_quota_exhausted",
        )

    async def _redirect_owner_invalid_report(
        self,
        message: discord.Message,
        *,
        bot_mention: str,
    ) -> None:
        await self._reply_deterministic_notice(
            message,
            content=build_owner_invalid_report_message(
                messages=self.persona.messages.access,
                message_id=message.id,
                bot_mention=bot_mention,
            ),
            event_prefix="report_owner_invalid_report",
        )

    async def _reply_deterministic_notice(
        self,
        message: discord.Message,
        *,
        content: str,
        event_prefix: str,
    ) -> None:
        guild = message.guild
        if guild is None:
            return
        cooldown_key = (guild.id, message.channel.id, message.author.id, event_prefix)
        now = monotonic()
        last_reply = self._deterministic_reply_cooldowns.get(cooldown_key)
        if last_reply is not None and now - last_reply < DETERMINISTIC_REPLY_COOLDOWN_SECONDS:
            log_event(
                LOGGER,
                "info",
                f"{event_prefix}_suppressed",
                guild_id=guild.id,
                channel_id=message.channel.id,
                message_id=message.id,
                user_id=message.author.id,
            )
            return

        self._deterministic_reply_cooldowns[cooldown_key] = now
        try:
            await message.reply(
                content=content,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log_event(
                LOGGER,
                "info",
                f"{event_prefix}_sent",
                guild_id=guild.id,
                channel_id=message.channel.id,
                message_id=message.id,
                user_id=message.author.id,
            )
        except (discord.Forbidden, discord.HTTPException) as error:
            self._deterministic_reply_cooldowns.pop(cooldown_key, None)
            log_event(
                LOGGER,
                "warning",
                f"{event_prefix}_failed",
                exc_info=error,
                guild_id=guild.id,
                channel_id=message.channel.id,
                message_id=message.id,
                user_id=message.author.id,
                **exception_fields(error),
            )

    @discord.slash_command(name="ayuda", description="Muestra las guías disponibles")
    async def review_help(self, ctx: discord.ApplicationContext) -> None:
        bot_user = self.bot.user
        extra_feature_lines: list[str] = [
            "• `/support`: apoyo voluntario al desarrollo y mantenimiento del bot."
        ]
        if self._config_provider is not None and ctx.guild is not None and ctx.channel is not None:
            with suppress(Exception):
                bundle = await self._config_provider.cached(ctx.guild.id)  # type: ignore[attr-defined]
                forum_feature = bundle.snapshot.features.get("forum_shortcuts")
                if forum_feature is not None and forum_feature.enabled:
                    extra_feature_lines.append(
                        "• `/foro`: accesos oficiales y trámites disponibles en el foro."
                    )
                resources_feature = bundle.snapshot.features.get("operational_resources")
                if (
                    resources_feature is not None
                    and resources_feature.enabled
                    and isinstance(ctx.author, discord.Member)
                    and any(
                        role.id in bundle.settings.operational_staff_role_ids
                        for role in ctx.author.roles
                    )
                ):
                    extra_feature_lines.append(
                        "• `/manual`: abre el manual oficial de la facción."
                    )
                evidence_feature = bundle.snapshot.features.get("arrest_report_lookup")
                if (
                    evidence_feature is not None
                    and evidence_feature.enabled
                    and isinstance(ctx.author, discord.Member)
                    and any(
                        role.id in bundle.settings.operational_staff_role_ids for role in ctx.author.roles
                    )
                ):
                    extra_feature_lines.append(
                        "• `/ver-evidencia`: consulta una bolsa y su cadena de custodia."
                    )
                warrant_feature = bundle.snapshot.features.get("warrant_notifications")
                if (
                    warrant_feature is not None
                    and warrant_feature.enabled
                    and isinstance(ctx.author, discord.Member)
                    and any(
                        role.id in bundle.settings.operational_staff_role_ids
                        for role in ctx.author.roles
                    )
                ):
                    extra_feature_lines.append(
                        "• `/info-ordenes`: cómo notificar una orden a los encargados."
                    )
                feature = bundle.snapshot.features.get("logbook")
                if (
                    feature is not None
                    and feature.enabled
                    and ctx.channel.id in bundle.settings.logbook_channel_ids
                ):
                    extra_feature_lines.extend(
                        (
                            "• `/info-bitacora`: cómo registrar novedades estructuradas.",
                            "• `/guia-bitacora`: categorías y tipos disponibles.",
                        )
                    )
                if (
                    feature is not None
                    and feature.enabled
                    and isinstance(ctx.author, discord.Member)
                    and any(
                        role.id in bundle.settings.faction_report_role_ids
                        for role in ctx.author.roles
                    )
                ):
                    extra_feature_lines.append(
                        "• `/resumen-bitacora`: resume registros por período, categoría y tipo."
                    )
                radio_feature = bundle.snapshot.features.get("radio_audit")
                if (
                    radio_feature is not None
                    and radio_feature.enabled
                    and isinstance(ctx.author, discord.Member)
                ):
                    member_role_ids = {role.id for role in ctx.author.roles}
                    if member_role_ids.intersection(bundle.settings.operational_staff_role_ids):
                        extra_feature_lines.append(
                            "• `/radios-faccion`: consulta registros de radio por fecha, slot y miembro."
                        )
                    if member_role_ids.intersection(bundle.settings.radio_audit_role_ids):
                        extra_feature_lines.append(
                            "• `/auditar-radio`: analiza el uso del slot 1 según el manual."
                        )
        embed = build_help_embed(
            persona=self.persona,
            avatar_url=str(bot_user.display_avatar.url) if bot_user else None,
            extra_feature_guides=("\n".join(extra_feature_lines) if extra_feature_lines else None),
        )
        await ctx.respond(embed=embed, ephemeral=False)

    @discord.slash_command(
        name="info-reportes",
        description="Explica cómo revisar tu propio reporte",
    )
    async def report_info(self, ctx: discord.ApplicationContext) -> None:
        bot_user = self.bot.user
        interaction_id = getattr(getattr(ctx, "interaction", None), "id", 0)
        if self.access_policy.blocks_member(ctx.author):
            await ctx.respond(
                embed=discord.Embed(
                    title="⚠️ Acceso restringido",
                    description=build_blocked_user_message(
                        messages=self.persona.messages.access,
                        message_id=interaction_id,
                        form_of_address=self.persona.identity.default_form_of_address,
                    ),
                    color=discord.Color.red(),
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        owner_access_enabled = getattr(self.settings, "report_owner_access_max_rounds", None) is not None
        if not owner_access_enabled and not self.access_policy.allows_member(ctx.author):
            await ctx.respond(
                embed=discord.Embed(
                    title="⚠️ Acceso restringido",
                    description=build_unauthorized_user_message(
                        messages=self.persona.messages.access,
                        message_id=interaction_id,
                        form_of_address=self.persona.identity.default_form_of_address,
                    ),
                    color=discord.Color.red(),
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        report_channel_ids = self.settings.report_channel_allowlist.ids
        if report_channel_ids is not None:
            report_channel_ids = frozenset(
                await self._resolve_report_channels(
                    guild=ctx.guild,
                    requester=ctx.author,
                    channel_ids=report_channel_ids,
                )
            )
        await ctx.respond(
            embed=build_report_info_embed(
                persona=self.persona,
                bot_mention=(bot_user.mention if bot_user else self.persona.identity.display_name),
                report_channel_ids=report_channel_ids,
                report_owner_max_rounds=getattr(
                    self.settings,
                    "report_owner_access_max_rounds",
                    None,
                ),
                avatar_url=str(bot_user.display_avatar.url) if bot_user else None,
            ),
            ephemeral=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        log_event(
            LOGGER,
            "info",
            "report_info_opened",
            guild_id=ctx.guild.id if ctx.guild else "direct_message",
            channel_id=ctx.channel.id if ctx.channel else "unknown",
            user_id=ctx.author.id,
        )

    @discord.slash_command(name="guia", description="Consulta la guía de reportes por secciones")
    async def report_guide(self, ctx: discord.ApplicationContext) -> None:
        view = GuideNavigationView(
            requester_id=ctx.author.id,
            guide=self.service.guide,
            display_name=self.persona.identity.display_name,
            messages=self.persona.messages.access,
            ui=self.persona.messages.interface.knowledge,
        )
        await ctx.respond(
            embed=view.current_embed,
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        log_event(
            LOGGER,
            "info",
            "report_guide_opened",
            guild_id=ctx.guild.id if ctx.guild else "direct_message",
            user_id=ctx.author.id,
        )

    @discord.slash_command(
        name="ts3",
        description="Muestra la dirección oficial de TeamSpeak 3",
    )
    async def teamspeak_server(self, ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None:
            await ctx.respond(
                embed=discord.Embed(
                    title="⚠️ TeamSpeak no disponible",
                    description="Este comando solo funciona dentro de un servidor.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return
        role_ids = set(getattr(self.settings, "operational_staff_role_ids", ()))
        member_roles = getattr(ctx.author, "roles", ())
        if not role_ids or not any(role.id in role_ids for role in member_roles):
            copy = self.persona.messages.interface.commands
            await ctx.respond(
                embed=discord.Embed(
                    title=copy.teamspeak_unauthorized_title,
                    description=copy.teamspeak_unauthorized,
                    color=discord.Color.red(),
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log_event(
                LOGGER,
                "info",
                "teamspeak_address_denied",
                guild_id=ctx.guild.id,
                channel_id=ctx.channel.id if ctx.channel else "unknown",
                user_id=ctx.author.id,
            )
            return
        await ctx.respond(
            embed=build_teamspeak_embed(
                persona=self.persona,
                address=self.settings.teamspeak_address
                if isinstance(self.settings, GuildRuntimeSettings)
                else TEAMSPEAK_SERVER_ADDRESS,
            ),
            ephemeral=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        log_event(
            LOGGER,
            "info",
            "teamspeak_address_shared",
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id if ctx.channel else "unknown",
            user_id=ctx.author.id,
        )

    @discord.slash_command(
        name="manual",
        description="Publica el enlace al manual oficial de la facción",
    )
    async def faction_manual(self, ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None:
            await ctx.respond(
                embed=discord.Embed(
                    title="⚠️ Manual no disponible",
                    description="Este comando solo funciona dentro de un servidor.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return
        role_ids = set(getattr(self.settings, "operational_staff_role_ids", ()))
        member_roles = getattr(ctx.author, "roles", ())
        copy = self.persona.messages.interface.operational_resources
        if not role_ids or not any(role.id in role_ids for role in member_roles):
            await ctx.respond(
                embed=discord.Embed(
                    title=copy.alert_title,
                    description=copy.unauthorized,
                    color=discord.Color.red(),
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log_event(
                LOGGER,
                "info",
                "faction_manual_denied",
                guild_id=ctx.guild.id,
                channel_id=ctx.channel.id if ctx.channel else "unknown",
                user_id=ctx.author.id,
            )
            return
        options = getattr(self.settings, "operational_resources_options", None)
        if options is None:
            await ctx.respond(
                embed=discord.Embed(
                    title=copy.alert_title,
                    description=copy.unavailable,
                    color=discord.Color.red(),
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await ctx.respond(
            embed=build_manual_embed(
                organization_name=self.persona.identity.organization_name,
                organization_acronym=self.persona.identity.organization_acronym,
                manual_url=str(options.manual_url),
            ),
            ephemeral=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        log_event(
            LOGGER,
            "info",
            "faction_manual_shared",
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id if ctx.channel else "unknown",
            user_id=ctx.author.id,
        )

    @discord.slash_command(name="cargos", description="Busca cargos en el código penal")
    async def charge_search(self, ctx: discord.ApplicationContext) -> None:
        await ctx.send_modal(
            ChargeSearchModal(
                catalog=self.service.catalog,
                requester_id=ctx.author.id,
                display_name=self.persona.identity.display_name,
                access_messages=self.persona.messages.access,
                failure_messages=self.persona.messages.failures,
                ui=self.persona.messages.interface.knowledge,
                navigation=self.persona.messages.interface.navigation,
            )
        )

    @discord.slash_command(name="faq", description="Consulta preguntas frecuentes sobre reportes")
    async def report_faq(self, ctx: discord.ApplicationContext) -> None:
        view = FaqNavigationView(
            requester_id=ctx.author.id,
            display_name=self.persona.identity.display_name,
            messages=self.persona.messages.access,
            ui=self.persona.messages.interface.knowledge,
        )
        await ctx.respond(
            embed=view.current_embed,
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        log_event(
            LOGGER,
            "info",
            "report_faq_opened",
            guild_id=ctx.guild.id if ctx.guild else "direct_message",
            user_id=ctx.author.id,
        )

    @discord.slash_command(name="pendientes", description="Consulta reportes pendientes de supervisión")
    async def supervisor_pending(self, ctx: discord.ApplicationContext) -> None:
        identity = await self._authorize_supervisor_command(ctx)
        if identity is None:
            return
        guild, member = identity
        channels = self.personal_report_finder.visible_channels(guild, member)
        if not channels:
            await ctx.respond(
                self.persona.messages.access.no_supervisor_channels,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await ctx.send_modal(
            PendingReportsModal(
                finder=self.personal_report_finder,
                policy=self.supervisor_policy,
                guild=guild,
                member=member,
                channels=channels,
                display_name=self.persona.identity.display_name,
                access_messages=self.persona.messages.access,
                failure_messages=self.persona.messages.failures,
                ui=self.persona.messages.interface.supervision,
                navigation=self.persona.messages.interface.navigation,
            )
        )

    @discord.slash_command(name="ranking", description="Compara resultados de reportes por miembro")
    async def supervisor_ranking(self, ctx: discord.ApplicationContext) -> None:
        identity = await self._authorize_supervisor_command(ctx)
        if identity is None:
            return
        guild, member = identity
        channels = self.personal_report_finder.visible_channels(guild, member)
        if not channels:
            await ctx.respond(
                self.persona.messages.access.no_supervisor_channels,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await ctx.send_modal(
            RankingModal(
                finder=self.personal_report_finder,
                policy=self.supervisor_policy,
                guild=guild,
                member=member,
                channels=channels,
                display_name=self.persona.identity.display_name,
                access_messages=self.persona.messages.access,
                failure_messages=self.persona.messages.failures,
                ui=self.persona.messages.interface.supervision,
                navigation=self.persona.messages.interface.navigation,
            )
        )

    @discord.slash_command(
        name="verguenza",
        description="Muestra quién acumula más reportes sin aprobar",
    )
    async def open_report_shame(self, ctx: discord.ApplicationContext) -> None:
        identity = await self._authorize_report_analytics_command(ctx)
        if identity is None:
            return
        guild, member = identity
        if not self.personal_report_finder.visible_channels(guild, member):
            await ctx.respond(
                self.persona.messages.access.no_personal_channels,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await ctx.send_modal(
            ShameRankingModal(
                finder=self.personal_report_finder,
                guild=guild,
                member=member,
                display_name=self.persona.identity.display_name,
                access_messages=self.persona.messages.access,
                failure_messages=self.persona.messages.failures,
                ui=self.persona.messages.interface.supervision,
                navigation=self.persona.messages.interface.navigation,
            )
        )

    @discord.slash_command(
        name="honor",
        description="Destaca a quienes acumulan más reportes aprobados",
    )
    async def approved_report_honor(self, ctx: discord.ApplicationContext) -> None:
        identity = await self._authorize_report_analytics_command(ctx)
        if identity is None:
            return
        guild, member = identity
        if not self.personal_report_finder.visible_channels(guild, member):
            await ctx.respond(
                self.persona.messages.access.no_personal_channels,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await ctx.send_modal(
            HonorRankingModal(
                finder=self.personal_report_finder,
                guild=guild,
                member=member,
                display_name=self.persona.identity.display_name,
                access_messages=self.persona.messages.access,
                failure_messages=self.persona.messages.failures,
                ui=self.persona.messages.interface.supervision,
                navigation=self.persona.messages.interface.navigation,
            )
        )

    @discord.slash_command(
        name="errores-reportes",
        description="Muestra los errores más frecuentes en reportes",
    )
    async def report_errors(self, ctx: discord.ApplicationContext) -> None:
        identity = await self._authorize_report_analytics_command(ctx)
        if identity is None:
            return
        guild, member = identity
        channels = self.personal_report_finder.visible_channels(guild, member)
        if not channels:
            await ctx.respond(
                self.persona.messages.access.no_personal_channels,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if self.report_review_repository is None:
            await ctx.respond(
                self.persona.messages.failures.supervision_query_failure,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await ctx.send_modal(
            ReportErrorsModal(
                repository=self.report_review_repository,
                policy=self.report_analytics_policy,
                guild=guild,
                member=member,
                channels=channels,
                display_name=self.persona.identity.display_name,
                access_messages=self.persona.messages.access,
                failure_messages=self.persona.messages.failures,
                ui=self.persona.messages.interface.supervision,
                navigation=self.persona.messages.interface.navigation,
            )
        )

    @discord.slash_command(name="mis-reportes", description="Busca tus reportes y abre sus publicaciones")
    async def personal_reports(self, ctx: discord.ApplicationContext) -> None:
        identity = await self._authorize_personal_command(ctx)
        if identity is None:
            return
        guild, member = identity
        channels = self.personal_report_finder.visible_channels(guild, member)
        if not channels:
            await ctx.respond(
                self.persona.messages.access.no_personal_channels,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await ctx.send_modal(
            PersonalReportsModal(
                finder=self.personal_report_finder,
                guild=guild,
                member=member,
                channels=channels,
                display_name=self.persona.identity.display_name,
                access_messages=self.persona.messages.access,
                failure_messages=self.persona.messages.failures,
                ui=self.persona.messages.interface.personal_reports,
                navigation=self.persona.messages.interface.navigation,
            )
        )

    @discord.slash_command(name="mi-resumen", description="Muestra tu resumen privado del mes actual")
    async def personal_summary(self, ctx: discord.ApplicationContext) -> None:
        identity = await self._authorize_personal_command(ctx)
        if identity is None:
            return
        guild, member = identity
        await ctx.defer(ephemeral=True)
        try:
            result = await self.personal_report_finder.find(
                guild,
                member,
                PersonalReportQuery(
                    period=PersonalReportPeriod.CURRENT_MONTH,
                    status=PersonalReportStatusFilter.ALL,
                ),
            )
            await ctx.send_followup(
                embed=build_personal_summary_embed(
                    result,
                    display_name=self.persona.identity.display_name,
                    ui=self.persona.messages.interface.personal_reports,
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log_event(
                LOGGER,
                "info",
                "personal_report_summary_completed",
                guild_id=guild.id,
                user_id=member.id,
                result_count=len(result.reports),
                truncated=result.truncated,
            )
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "personal_report_summary_failed",
                exc_info=error,
                guild_id=guild.id,
                user_id=member.id,
                interaction_id=ctx.interaction.id,
                **exception_fields(error),
            )
            await ctx.send_followup(
                self.persona.messages.failures.personal_summary_failure,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _authorize_personal_command(
        self,
        ctx: discord.ApplicationContext,
    ) -> tuple[discord.Guild, discord.Member] | None:
        """Apply the central guild/user allowlists before reading Discord history."""

        guild = ctx.guild
        member = ctx.author
        if guild is None or not self.access_policy.guilds.allows(guild.id):
            await ctx.respond(
                self.persona.messages.access.personal_wrong_guild,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return None
        if not isinstance(member, discord.Member):
            await ctx.respond(
                self.persona.messages.access.personal_member_unavailable,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return None
        if not self.access_policy.users.allows(member.id):
            await ctx.respond(
                build_unauthorized_user_message(
                    messages=self.persona.messages.access,
                    message_id=ctx.interaction.id,
                    form_of_address=self.persona.identity.default_form_of_address,
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log_event(
                LOGGER,
                "info",
                "personal_report_command_blocked",
                guild_id=guild.id,
                user_id=member.id,
                interaction_id=ctx.interaction.id,
            )
            return None
        return guild, member

    async def _authorize_report_analytics_command(
        self,
        ctx: discord.ApplicationContext,
    ) -> tuple[discord.Guild, discord.Member] | None:
        """Restrict global report analytics to the dedicated staff role policy."""

        guild = ctx.guild
        member = ctx.author
        if guild is None or not self.access_policy.guilds.allows(guild.id):
            await ctx.respond(
                self.persona.messages.access.personal_wrong_guild,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return None
        if not isinstance(member, discord.Member):
            await ctx.respond(
                self.persona.messages.access.personal_member_unavailable,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return None
        if not self.report_analytics_policy.allows(member):
            await ctx.respond(
                build_supervisor_unauthorized_message(self.persona.messages.access),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log_event(
                LOGGER,
                "info",
                "report_analytics_command_blocked",
                guild_id=guild.id,
                user_id=member.id,
                interaction_id=ctx.interaction.id,
            )
            return None
        return guild, member

    async def _authorize_supervisor_command(
        self,
        ctx: discord.ApplicationContext,
    ) -> tuple[discord.Guild, discord.Member] | None:
        """Apply the guild and supervisor-role policies before reading report data."""

        guild = ctx.guild
        member = ctx.author
        if guild is None or not self.access_policy.guilds.allows(guild.id):
            await ctx.respond(
                self.persona.messages.access.supervisor_wrong_guild,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return None
        if not isinstance(member, discord.Member):
            await ctx.respond(
                self.persona.messages.access.supervisor_member_unavailable,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return None
        if not self.supervisor_policy.allows(member):
            await ctx.respond(
                build_supervisor_unauthorized_message(self.persona.messages.access),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log_event(
                LOGGER,
                "info",
                "supervisor_command_blocked",
                guild_id=guild.id,
                user_id=member.id,
                interaction_id=ctx.interaction.id,
            )
            return None
        return guild, member
