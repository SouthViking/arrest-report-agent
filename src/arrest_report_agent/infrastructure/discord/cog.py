"""Discord event listener and public help command."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager, suppress

import discord
from discord.ext import commands

from arrest_report_agent.application import ReviewRunContext, ReviewService
from arrest_report_agent.application.errors import public_error_message
from arrest_report_agent.config import AppSettings
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.domain import ErrorKind
from arrest_report_agent.domain.errors import ContextUnavailableError

from .context import DiscordContextBuilder, render_agent_input
from .publisher import DiscordReviewPublisher
from .trigger import should_review_message

LOGGER = logging.getLogger(__name__)

PENAL_CODE_URL = "https://ucp.sarp.es/app/ucp/penalcode"
PROCEDURES_MANUAL_URL = (
    "https://docs.google.com/document/d/1DrhSgzPbdLoe0ahFTVrNPb_zBPuhVeatxW5Hs8ECMHE/"
    "edit?pli=1&tab=t.0#heading=h.thzh3oj2x0rk"
)


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
    channel_ids: frozenset[int],
    avatar_url: str | None = None,
) -> discord.Embed:
    """Introduce the reviewer and explain its workflow in character."""

    channels = "\n".join(f"• <#{channel_id}>" for channel_id in sorted(channel_ids))
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
            "del mensaje. Si ya existe un hilo, puedes volver a mencionarme allí: leeré el "
            "historial reciente para continuar la revisión."
        ),
        inline=False,
    )
    embed.add_field(
        name="🧵 Qué haré con el expediente",
        value=(
            "Marcaré la revisión en curso con ⏳. Si está correcta, dejaré ✅; "
            "si requiere ajustes, dejaré ❌ y abriré un hilo con observaciones claras para "
            "que podamos iterar sin llenar el canal de papeleo."
        ),
        inline=False,
    )
    embed.add_field(
        name="📚 Documentación oficial",
        value=(
            f"• [Código Penal]({PENAL_CODE_URL})\n"
            f"• [Manual de Procedimientos]({PROCEDURES_MANUAL_URL})"
        ),
        inline=False,
    )
    embed.set_footer(text="No intervengo sin una mención. El papeleo no se aprueba solo, deputy.")
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    return embed


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
        self.context_builder = DiscordContextBuilder(settings)
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_reviews)
        self._locks: dict[int, asyncio.Lock] = {}
        self._attempts: defaultdict[int, int] = defaultdict(int)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        log_event(
            LOGGER,
            "info",
            "discord_ready",
            bot_user_id=self.bot.user.id if self.bot.user else "unknown",
            guild_id=self.settings.discord_guild_id,
            configured_channels=len(self.settings.review_channel_ids),
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        bot_user = self.bot.user
        if bot_user is None or not should_review_message(message, bot_user.id, self.settings):
            return
        try:
            target = await self.context_builder.resolve_target(message)
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
            async with lock, maintain_typing_indicator(message.channel), self._semaphore:
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
                )
                agent_input = render_agent_input(case, self.settings.openai_image_detail)
                await self.service.review(run_context, agent_input)
        finally:
            if not lock.locked():
                self._locks.pop(root_id, None)

    @wv.command(name="help", description="Explica cómo solicitar una revisión")
    async def review_help(self, ctx: discord.ApplicationContext) -> None:
        bot_user = self.bot.user
        embed = build_help_embed(
            display_name=self.settings.identity.display_name,
            role_title=self.settings.identity.role_title,
            bot_mention=bot_user.mention if bot_user else self.settings.identity.display_name,
            channel_ids=self.settings.review_channel_ids,
            avatar_url=str(bot_user.display_avatar.url) if bot_user else None,
        )
        await ctx.respond(embed=embed, ephemeral=False)
