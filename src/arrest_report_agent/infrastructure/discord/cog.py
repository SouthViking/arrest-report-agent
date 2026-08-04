"""Discord event listener and deterministic slash commands."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic

import discord
from discord.ext import commands

from arrest_report_agent.application import (
    PersonalReportPeriod,
    PersonalReportQuery,
    PersonalReportStatusFilter,
    ReviewRunContext,
    ReviewService,
)
from arrest_report_agent.application.errors import public_error_message
from arrest_report_agent.config import AppSettings
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.domain import ErrorKind
from arrest_report_agent.domain.errors import ContextUnavailableError

from .context import DiscordContextBuilder, render_agent_input
from .general_knowledge import (
    PENAL_CODE_URL,
    PROCEDURES_MANUAL_URL,
    ChargeSearchModal,
    FaqNavigationView,
    GuideNavigationView,
)
from .personal_reports import (
    DiscordPersonalReportFinder,
    PersonalReportsModal,
    build_personal_summary_embed,
)
from .presence import ReviewPresence
from .publisher import DiscordReviewPublisher
from .trigger import DiscordAccessPolicy, TriggerDecision

LOGGER = logging.getLogger(__name__)

DETERMINISTIC_REPLY_COOLDOWN_SECONDS = 30.0
WRONG_CHANNEL_INTROS = (
    "En este canal no estoy de guardia.",
    "Este canal queda fuera de mi ronda asignada.",
    "Aquí no tengo habilitada la atención.",
    "Me encontró, pero en la oficina equivocada.",
)
UNAUTHORIZED_USER_INTROS = (
    "Su placa no figura en el libro de guardia.",
    "No tengo su nombre entre el personal habilitado para esta mesa.",
    "La orden del turno no me permite atender solicitudes asociadas a su placa.",
    "El listado de servicio no reconoce su autorización para esta oficina.",
)


class ReviewChannelAvailability(StrEnum):
    AVAILABLE = "available"
    HIDDEN = "hidden"
    UNAVAILABLE = "unavailable"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class ReviewChannelResolution:
    visible_ids: tuple[int, ...]
    availability: ReviewChannelAvailability


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
    display_name: str,
    role_title: str,
    bot_mention: str,
    channel_ids: frozenset[int] | None,
    avatar_url: str | None = None,
) -> discord.Embed:
    """Introduce the reviewer and explain its workflow in character."""

    channels = (
        "\n".join(f"• <#{channel_id}>" for channel_id in sorted(channel_ids))
        if channel_ids is not None
        else "• Todos los canales del servidor"
    )
    embed = discord.Embed(
        title=f"⭐ {display_name} · {role_title}",
        description=(
            f"Soy **{display_name}**, el **{role_title}** asignado a este turno. "
            "Mi trabajo es revisar expedientes de arresto antes de que acumulen polvo… "
            "o correcciones del mando."
        ),
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="📋 Qué reviso",
        value=(
            "• Narrativa, cronología y coherencia del procedimiento.\n"
            "• Fotografías, evidencias y consistencia visual del expediente.\n"
            "• Cargos declarados frente al catálogo y la guía oficial."
        ),
        inline=False,
    )
    embed.add_field(
        name="📍 Dónde estoy de guardia",
        value=channels,
        inline=False,
    )
    embed.add_field(
        name="📣 Cómo solicitar una revisión",
        value=(
            f"Publica el reporte con sus adjuntos e incluye a {bot_mention} en cualquier parte "
            "del mensaje: abriré un hilo para mantener despejado el canal. Si respondes "
            "directamente a uno de mis mensajes en el canal, contestaré allí mismo."
        ),
        inline=False,
    )
    embed.add_field(
        name="🧵 Qué haré con el expediente",
        value=(
            "Dentro de un hilo, las menciones y respuestas seguirán en ese mismo lugar y leeré "
            "el historial reciente. Al revisar, marcaré ⏳; si está correcto dejaré ✅ y, si "
            "requiere ajustes, ❌ con observaciones claras."
        ),
        inline=False,
    )
    embed.add_field(
        name="🗂️ Consultas privadas",
        value=(
            "• `/wv mis-reportes`: filtra tus publicaciones y abre rápidamente cada expediente.\n"
            "• `/wv mi-resumen`: consulta tus cifras del mes actual."
        ),
        inline=False,
    )
    embed.add_field(
        name="📚 Consultas generales",
        value=(
            "• `/wv guia`: consulta las reglas de redacción y revisión por sección.\n"
            "• `/wv cargos`: busca delitos y sanciones en el catálogo local.\n"
            "• `/wv faq`: resuelve dudas frecuentes del procedimiento."
        ),
        inline=False,
    )
    embed.add_field(
        name="📚 Documentación oficial",
        value=(f"• [Código Penal]({PENAL_CODE_URL})\n• [Manual de Procedimientos]({PROCEDURES_MANUAL_URL})"),
        inline=False,
    )
    embed.set_footer(
        text="Intervengo con una mención o una respuesta directa. El papeleo no se aprueba solo, deputy."
    )
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    return embed


def build_wrong_channel_message(
    *,
    channel_ids: tuple[int, ...],
    message_id: int,
    availability: ReviewChannelAvailability = ReviewChannelAvailability.UNAVAILABLE,
) -> str:
    """Build a concise rich-text redirect without using AI."""

    intro = WRONG_CHANNEL_INTROS[message_id % len(WRONG_CHANNEL_INTROS)]
    heading = f"📍 **Fuera de mi zona de guardia, deputy.** {intro}\n"
    if channel_ids:
        channel_mentions = [f"<#{channel_id}>" for channel_id in sorted(channel_ids)]
        channels = (
            channel_mentions[0]
            if len(channel_mentions) == 1
            else f"{', '.join(channel_mentions[:-1])} o {channel_mentions[-1]}"
        )
        return heading + f"Puede encontrarme en {channels}; mencióneme allí para continuar."
    if availability is ReviewChannelAvailability.HIDDEN:
        return (
            heading
            + "No tiene acceso visible a mis canales asignados. Consulte con un mando para continuar."
        )
    if availability is ReviewChannelAvailability.UNCERTAIN:
        return (
            heading
            + "No pude confirmar ningún canal de guardia disponible. Avise a un administrador "
            "para revisar la configuración."
        )
    return (
        heading
        + "Mis canales asignados ya no están disponibles. Avise a un administrador para revisar "
        "la configuración."
    )


def build_unauthorized_user_message(*, message_id: int) -> str:
    """Build a concise in-character access notice without invoking AI."""

    intro = UNAUTHORIZED_USER_INTROS[message_id % len(UNAUTHORIZED_USER_INTROS)]
    return (
        f"🔒 **Un momento, deputy.** {intro}\n"
        "Consulte con un mando para que revise su habilitación antes de continuar."
    )


class ReviewCog(commands.Cog):
    wv = discord.SlashCommandGroup("wv", "Operaciones del revisor de reportes")

    def __init__(
        self,
        bot: discord.Bot,
        settings: AppSettings,
        service: ReviewService,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.service = service
        self.access_policy = DiscordAccessPolicy.from_settings(settings)
        self.context_builder = DiscordContextBuilder(settings, bot)
        self.personal_report_finder = DiscordPersonalReportFinder(bot, settings)
        self.presence = ReviewPresence(
            bot,
            idle_text=settings.bot_idle_activity,
            conversation_text=settings.bot_conversation_activity,
            knowledge_text=settings.bot_knowledge_activity,
            review_text=settings.bot_review_activity,
        )
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_reviews)
        self._locks: dict[int, asyncio.Lock] = {}
        self._attempts: defaultdict[int, int] = defaultdict(int)
        self._deterministic_reply_cooldowns: dict[tuple[int, int, int], float] = {}
        self._missing_review_channel_ids: set[int] = set()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self.presence.synchronize()
        log_event(
            LOGGER,
            "info",
            "discord_ready",
            bot_user_id=self.bot.user.id if self.bot.user else "unknown",
            guild_access_mode=("restricted" if self.access_policy.guilds.is_restricted else "open"),
            configured_guilds=self.access_policy.guilds.configured_count,
            channel_access_mode=("restricted" if self.access_policy.channels.is_restricted else "open"),
            configured_channels=self.access_policy.channels.configured_count,
            user_access_mode=("restricted" if self.access_policy.users.is_restricted else "open"),
            configured_users=self.access_policy.users.configured_count,
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        bot_user = self.bot.user
        if bot_user is None:
            return
        evaluation = await self.access_policy.evaluate_event(message, bot_user.id)
        decision = evaluation.decision
        if decision is TriggerDecision.REDIRECT_UNAUTHORIZED_USER:
            await self._redirect_unauthorized_user(message)
            return
        if decision is TriggerDecision.REDIRECT_WRONG_CHANNEL:
            await self._redirect_wrong_channel(message)
            return
        if decision is not TriggerDecision.REVIEW:
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
                publisher = DiscordReviewPublisher(self.bot, self.settings, target)
                try:
                    case = await self.context_builder.build_case(target)
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
                            public_message=public_error_message(ErrorKind.CONTEXT_UNAVAILABLE),
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
                run_context = ReviewRunContext(
                    case=case,
                    publisher=publisher,
                    attempt=attempt,
                    activity=activity,
                )
                agent_input = render_agent_input(
                    case,
                    self.service.model.runtime_defaults.image_detail,
                )
                await self.service.review(run_context, agent_input)
        finally:
            if not lock.locked():
                self._locks.pop(root_id, None)

    async def _redirect_wrong_channel(self, message: discord.Message) -> None:
        channel_ids = self.access_policy.channels.ids
        if channel_ids is None:
            return
        resolution = await self._resolve_review_channels(message, channel_ids)
        await self._reply_deterministic_notice(
            message,
            content=build_wrong_channel_message(
                channel_ids=resolution.visible_ids,
                message_id=message.id,
                availability=resolution.availability,
            ),
            event_prefix="wrong_channel_redirect",
        )

    async def _resolve_review_channels(
        self,
        message: discord.Message,
        channel_ids: frozenset[int],
    ) -> ReviewChannelResolution:
        """Resolve configured destinations without exposing stale or inaccessible references."""

        guild = message.guild
        if guild is None:
            return ReviewChannelResolution((), ReviewChannelAvailability.UNAVAILABLE)

        visible_ids: list[int] = []
        hidden_from_requester = False
        lookup_uncertain = False
        for channel_id in sorted(channel_ids):
            if channel_id in self._missing_review_channel_ids:
                continue
            channel = guild.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await guild.fetch_channel(channel_id)
                except discord.NotFound as error:
                    self._missing_review_channel_ids.add(channel_id)
                    log_event(
                        LOGGER,
                        "warning",
                        "configured_review_channel_missing",
                        exc_info=error,
                        guild_id=guild.id,
                        channel_id=channel_id,
                        **exception_fields(error),
                    )
                    continue
                except (discord.Forbidden, discord.HTTPException) as error:
                    lookup_uncertain = True
                    log_event(
                        LOGGER,
                        "warning",
                        "configured_review_channel_lookup_failed",
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
                    "configured_review_channel_unsupported",
                    guild_id=guild.id,
                    channel_id=channel_id,
                    channel_type=type(channel).__name__,
                )
                continue

            bot_member = guild.me
            if bot_member is None or not channel.permissions_for(bot_member).view_channel:
                lookup_uncertain = True
                log_event(
                    LOGGER,
                    "warning",
                    "configured_review_channel_inaccessible",
                    guild_id=guild.id,
                    channel_id=channel_id,
                )
                continue
            if not channel.permissions_for(message.author).view_channel:
                hidden_from_requester = True
                continue
            visible_ids.append(channel_id)

        if visible_ids:
            availability = ReviewChannelAvailability.AVAILABLE
        elif hidden_from_requester:
            availability = ReviewChannelAvailability.HIDDEN
        elif lookup_uncertain:
            availability = ReviewChannelAvailability.UNCERTAIN
        else:
            availability = ReviewChannelAvailability.UNAVAILABLE
        return ReviewChannelResolution(tuple(visible_ids), availability)

    async def _redirect_unauthorized_user(self, message: discord.Message) -> None:
        await self._reply_deterministic_notice(
            message,
            content=build_unauthorized_user_message(message_id=message.id),
            event_prefix="unauthorized_user_redirect",
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
        cooldown_key = (guild.id, message.channel.id, message.author.id)
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

    @wv.command(name="help", description="Explica cómo solicitar una revisión")
    async def review_help(self, ctx: discord.ApplicationContext) -> None:
        bot_user = self.bot.user
        embed = build_help_embed(
            display_name=self.settings.identity.display_name,
            role_title=self.settings.identity.role_title,
            bot_mention=bot_user.mention if bot_user else self.settings.identity.display_name,
            channel_ids=self.access_policy.channels.ids,
            avatar_url=str(bot_user.display_avatar.url) if bot_user else None,
        )
        await ctx.respond(embed=embed, ephemeral=False)

    @wv.command(name="guia", description="Consulta la guía de reportes por secciones")
    async def report_guide(self, ctx: discord.ApplicationContext) -> None:
        view = GuideNavigationView(
            requester_id=ctx.author.id,
            guide=self.service.guide,
            display_name=self.settings.identity.display_name,
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

    @wv.command(name="cargos", description="Busca cargos en el código penal")
    async def charge_search(self, ctx: discord.ApplicationContext) -> None:
        await ctx.send_modal(
            ChargeSearchModal(
                catalog=self.service.catalog,
                requester_id=ctx.author.id,
                display_name=self.settings.identity.display_name,
            )
        )

    @wv.command(name="faq", description="Consulta preguntas frecuentes sobre reportes")
    async def report_faq(self, ctx: discord.ApplicationContext) -> None:
        view = FaqNavigationView(
            requester_id=ctx.author.id,
            display_name=self.settings.identity.display_name,
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

    @wv.command(name="mis-reportes", description="Busca tus reportes y abre sus publicaciones")
    async def personal_reports(self, ctx: discord.ApplicationContext) -> None:
        identity = await self._authorize_personal_command(ctx)
        if identity is None:
            return
        guild, member = identity
        channels = self.personal_report_finder.visible_channels(guild, member)
        if not channels:
            await ctx.respond(
                "📁 No tengo canales de reportes visibles disponibles para esta consulta.",
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
                display_name=self.settings.identity.display_name,
            )
        )

    @wv.command(name="mi-resumen", description="Muestra tu resumen privado del mes actual")
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
                    display_name=self.settings.identity.display_name,
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
                "📁 No pude preparar su resumen en este momento. Inténtelo nuevamente.",
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
                "📍 Este archivo personal solo está disponible en un servidor asignado.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return None
        if not isinstance(member, discord.Member):
            await ctx.respond(
                "📁 No pude identificar su ficha dentro del servidor.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return None
        if not self.access_policy.users.allows(member.id):
            await ctx.respond(
                build_unauthorized_user_message(message_id=ctx.interaction.id),
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
