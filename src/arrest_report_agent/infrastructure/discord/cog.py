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

PENAL_CODE_URL = "https://ucp.sarp.es/app/ucp/penalcode"
PROCEDURES_MANUAL_URL = (
    "https://docs.google.com/document/d/1DrhSgzPbdLoe0ahFTVrNPb_zBPuhVeatxW5Hs8ECMHE/"
    "edit?pli=1&tab=t.0#heading=h.thzh3oj2x0rk"
)

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

PERMISSION_LABELS = {
    "view_channel": "ver canal",
    "read_message_history": "leer historial",
    "send_messages": "enviar mensajes",
    "add_reactions": "añadir reacciones",
    "create_public_threads": "crear hilos públicos",
    "send_messages_in_threads": "escribir en hilos",
    "manage_threads": "gestionar hilos",
    "embed_links": "insertar enlaces",
}

EMBED_SPACER = "\u200b"


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


def build_status_embed(
    *,
    display_name: str,
    discord_ready: bool,
    environment: str,
    model: str,
    charge_count: int,
    guide_version: str,
    active_reviews: int,
    channel_permission_lines: tuple[str, ...],
) -> discord.Embed:
    """Build a readable two-column operational status embed."""

    embed = discord.Embed(
        title=f"⭐ Estado · {display_name}",
        color=discord.Color.green() if discord_ready else discord.Color.orange(),
    )
    rows = (
        (("Discord", "conectado" if discord_ready else "iniciando"), ("Entorno", environment)),
        (("Modelo", model), ("Catálogo", f"{charge_count} delitos")),
        (("Guía", guide_version), ("Revisiones activas", str(active_reviews))),
    )
    for left, right in rows:
        embed.add_field(name=left[0], value=left[1], inline=True)
        embed.add_field(name=EMBED_SPACER, value=EMBED_SPACER, inline=True)
        embed.add_field(name=right[0], value=right[1], inline=True)
    embed.add_field(
        name="🛡️ Canales y permisos",
        value="\n".join(channel_permission_lines)[:1024],
        inline=False,
    )
    return embed


class ReviewCog(commands.Cog):
    wv = discord.SlashCommandGroup("wv", "Operaciones del revisor de reportes")

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

    @wv.command(name="status", description="Muestra el estado operativo del revisor")
    async def review_status(self, ctx: discord.ApplicationContext) -> None:
        if not self._is_operator(ctx):
            await ctx.respond("Este parte está reservado a supervisores.", ephemeral=True)
            return
        embed = build_status_embed(
            display_name=self.settings.identity.display_name,
            discord_ready=self.bot.is_ready(),
            environment=self.settings.app_environment,
            model=self.settings.openai_model,
            charge_count=self.catalog.charge_count,
            guide_version=self.guide.version,
            active_reviews=len(self._locks),
            channel_permission_lines=self._channel_permission_lines(),
        )
        await ctx.respond(embed=embed, ephemeral=True)

    def _channel_permission_lines(self) -> tuple[str, ...]:
        guild = self.bot.get_guild(self.settings.discord_guild_id)
        if guild is None or guild.me is None:
            return ("⚠️ No encuentro el servidor configurado en la caché.",)

        lines: list[str] = []
        for channel_id in sorted(self.settings.review_channel_ids):
            channel = guild.get_channel(channel_id)
            if channel is None:
                lines.append(f"❌ <#{channel_id}> — canal no encontrado")
                continue
            permissions = channel.permissions_for(guild.me)
            missing = [name for name in REQUIRED_PERMISSIONS if not getattr(permissions, name, False)]
            if missing:
                labels = ", ".join(PERMISSION_LABELS[name] for name in missing)
                lines.append(f"❌ {channel.mention} — faltan: {labels}")
            else:
                lines.append(f"✅ {channel.mention} — permisos completos")
        return tuple(lines) or ("⚠️ No hay canales de revisión configurados.",)

    def _is_operator(self, ctx: discord.ApplicationContext) -> bool:
        author = ctx.author
        if not isinstance(author, discord.Member):
            return False
        if author.guild_permissions.manage_guild:
            return True
        role_id = self.settings.manual_review_role_id
        return role_id is not None and any(role.id == role_id for role in author.roles)
