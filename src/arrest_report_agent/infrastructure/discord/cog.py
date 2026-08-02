"""Discord event listener and operational slash commands."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

import discord
from discord.ext import commands

from arrest_report_agent.application import ReviewRunContext, ReviewService
from arrest_report_agent.config import AppSettings
from arrest_report_agent.config.logging import log_event
from arrest_report_agent.domain import ErrorKind
from arrest_report_agent.domain.errors import ContextUnavailableError
from arrest_report_agent.knowledge import ChargeCatalog, ReportGuide

from .context import DiscordContextBuilder, render_agent_input
from .publisher import DiscordReviewPublisher
from .trigger import should_review_message

LOGGER = logging.getLogger(__name__)

REQUIRED_PERMISSIONS = (
    "view_channel",
    "read_message_history",
    "send_messages",
    "add_reactions",
    "create_public_threads",
    "send_messages_in_threads",
    "manage_threads",
    "embed_links",
)


class ReviewCog(commands.Cog):
    review = discord.SlashCommandGroup("review", "Operaciones del revisor de reportes")

    def __init__(
        self,
        bot: discord.Bot,
        settings: AppSettings,
        service: ReviewService,
        catalog: ChargeCatalog,
        guide: ReportGuide,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.service = service
        self.catalog = catalog
        self.guide = guide
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
        except ContextUnavailableError:
            log_event(
                LOGGER,
                "warning",
                "review_target_unavailable",
                message_id=message.id,
                channel_id=message.channel.id,
                user_id=message.author.id,
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
            async with lock, self._semaphore:
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
                        root_message_id=root_id,
                        trigger_message_id=message.id,
                        error_type=type(error).__name__,
                    )
                    try:
                        await publisher.publish_failure(
                            kind=ErrorKind.CONTEXT_UNAVAILABLE,
                            public_message=(
                                f"**{self.settings.identity.display_name} informa:** No pude "
                                "reconstruir el expediente y su historial. Verifica mis permisos "
                                "de lectura."
                            ),
                            technical_reference=f"{root_id}-{attempt}-context_unavailable",
                        )
                    except Exception as publication_error:
                        log_event(
                            LOGGER,
                            "error",
                            "context_failure_publication_failed",
                            root_message_id=root_id,
                            error_type=type(publication_error).__name__,
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

    @review.command(name="help", description="Explica cómo solicitar una revisión")
    async def review_help(self, ctx: discord.ApplicationContext) -> None:
        await ctx.respond(
            (
                f"Menciona a **{self.settings.identity.display_name}** en cualquier parte del mensaje "
                "del reporte o dentro de su hilo. Solo atiendo los canales configurados y nunca "
                "intervengo en publicaciones sin mención explícita."
            ),
            ephemeral=True,
        )

    @review.command(name="status", description="Muestra el estado operativo del revisor")
    async def review_status(self, ctx: discord.ApplicationContext) -> None:
        if not self._is_operator(ctx):
            await ctx.respond("Este parte está reservado a supervisores.", ephemeral=True)
            return
        embed = discord.Embed(
            title=f"Estado · {self.settings.identity.display_name}",
            color=discord.Color.green() if self.bot.is_ready() else discord.Color.orange(),
        )
        embed.add_field(name="Discord", value="conectado" if self.bot.is_ready() else "iniciando")
        embed.add_field(name="Entorno", value=self.settings.app_environment)
        embed.add_field(name="Modelo", value=self.settings.openai_model)
        embed.add_field(name="Catálogo", value=f"{self.catalog.charge_count} delitos")
        embed.add_field(name="Guía", value=self.guide.version)
        embed.add_field(name="Revisiones activas", value=str(len(self._locks)))
        await ctx.respond(embed=embed, ephemeral=True)

    @review.command(name="diagnose", description="Comprueba permisos en los canales configurados")
    async def review_diagnose(self, ctx: discord.ApplicationContext) -> None:
        if not self._is_operator(ctx):
            await ctx.respond("Este diagnóstico está reservado a supervisores.", ephemeral=True)
            return
        guild = self.bot.get_guild(self.settings.discord_guild_id)
        if guild is None or guild.me is None:
            await ctx.respond("No encuentro el servidor configurado en la caché.", ephemeral=True)
            return
        lines: list[str] = []
        for channel_id in sorted(self.settings.review_channel_ids):
            channel = guild.get_channel(channel_id)
            if channel is None:
                lines.append(f"❌ `{channel_id}` — canal no encontrado")
                continue
            permissions = channel.permissions_for(guild.me)
            missing = [name for name in REQUIRED_PERMISSIONS if not getattr(permissions, name, False)]
            if missing:
                lines.append(f"❌ **#{channel.name}** — faltan: {', '.join(missing)}")
            else:
                lines.append(f"✅ **#{channel.name}** — permisos completos")
        embed = discord.Embed(
            title=f"Diagnóstico · {self.settings.identity.display_name}",
            description="\n".join(lines)[:4096],
            color=discord.Color.blue(),
        )
        await ctx.respond(embed=embed, ephemeral=True)

    def _is_operator(self, ctx: discord.ApplicationContext) -> bool:
        author = ctx.author
        if not isinstance(author, discord.Member):
            return False
        if author.guild_permissions.manage_guild:
            return True
        role_id = self.settings.manual_review_role_id
        return role_id is not None and any(role.id == role_id for role in author.roles)

