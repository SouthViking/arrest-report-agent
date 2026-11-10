"""Public, local-only Discord command for the server's dedeo joke."""

from __future__ import annotations

import logging
import math
import secrets
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import discord
from discord.ext import commands

from arrest_report_agent.config import BotPersona
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider

LOGGER = logging.getLogger(__name__)
DEDEO_COOLDOWN_SECONDS = 30


class MonotonicClock(Protocol):
    """Clock contract used to keep the cooldown deterministic in tests."""

    def __call__(self) -> float: ...


class DedeoCooldown:
    """Small in-memory cooldown scoped independently to each guild member."""

    def __init__(
        self,
        seconds: int = DEDEO_COOLDOWN_SECONDS,
        *,
        clock: MonotonicClock = time.monotonic,
    ) -> None:
        self.seconds = seconds
        self.clock = clock
        self._deadlines: dict[tuple[int, int], float] = {}

    def acquire(self, *, guild_id: int, user_id: int) -> int | None:
        """Reserve one use, or return the rounded-up remaining wait."""

        current = self.clock()
        key = (guild_id, user_id)
        deadline = self._deadlines.get(key, 0.0)
        if deadline > current:
            return max(1, math.ceil(deadline - current))

        self._deadlines[key] = current + self.seconds
        return None


@dataclass(frozen=True, slots=True)
class DedeoCopy:
    """Bounded local copy selected from the active startup persona."""

    titles: tuple[str, ...]
    messages: tuple[str, ...]
    creator_defense_titles: tuple[str, ...]
    creator_defense_messages: tuple[str, ...]
    colors: tuple[int, ...]
    unavailable: str
    empty: str
    bot_target: str
    creator_self_target: str
    cooldown: str


@dataclass(frozen=True, slots=True)
class DedeoSelection:
    """Resolved target and any special creator-protection outcome."""

    target: discord.Member | None
    creator_defense: bool = False
    creator_self_target: bool = False


ROY_DEDEO_COPY = DedeoCopy(
    titles=(
        "☝️ Dedeado",
        "👉 Cagaste",
        "🎯 Víctima del dedo",
        "🚨 Dedeo confirmado",
        "🏆 Premio al dedeado",
        "🫵 Te tocó",
        "📋 Ficha sentenciada",
        "🎲 Mala cuea",
        "🧠 Decisión de mis dos neuronas",
        "🚪 La salida es por allá",
        "📉 Futuro laboral: ninguno",
        "🔨 Veredicto totalmente serio",
        "🛑 Dedeo administrativo",
        "🧹 Limpieza de plantilla",
        "🥇 Ganador del delete",
        "📢 Comunicado innecesario",
        "🪦 Descansa en píxeles",
        "🎪 Decisión institucional",
        "🤡 Resultado oficial",
        "🛰️ El sistema habló",
        "📦 Empaque sus cosas",
        "⚠️ Habilidad detectada: existir",
        "🗑️ Destino encontrado",
        "👆 El dedo no perdona",
    ),
    messages=(
        "**{target}** salió sorteado. dedeate nomás y no hagai show.",
        "el dedo cósmico apuntó a **{target}**. cagaste.",
        "**{target}**, te tocó por estadística. estadística inventada, obvio.",
        "después de una investigación seria (mentira), **{target}** queda dedeado.",
        "consulté a los expertos y eligieron a **{target}**. los expertos era yo.",
        "**{target}**, entrega la ficha y dedeate antes q empeore la cuestión.",
        "felicidades **{target}**, ganaste el premio q nadie quería. dedeate.",
        "no sé qué hiciste **{target}**, pero seguro alguna wea. dedeado.",
        "el sistema eligió a **{target}**. reclama en ventanilla 7, saco wea.",
        "**{target}** fue sorprendido existiendo. sanción: dedearse.",
        "la democracia habló: **{target}**. nadie votó pero da lo mismo.",
        "a **{target}** le tocó. motivos? sí.",
        "por unanimidad de mis dos neuronas, **{target}** queda dedeado.",
        "hoy el universo odia a **{target}**. dedeate con dignidad po.",
        "**{target}**, se acabó el recreo. proceda a dedearse y deje de llorar.",
        "revisé todos los antecedentes y no entendí ni una wea. dedeado **{target}**.",
        "**{target}** confió en su suerte y su suerte dijo: jódete.",
        "se buscaba talento, apareció **{target}** y cerramos el proceso.",
        "**{target}**, la ficha llamó. dijo q quiere desaparecer.",
        "el algoritmo detectó exceso de **{target}** en el servidor. toca dedeo.",
        "iba a elegir con criterio, pero terminé eligiendo a **{target}**.",
        "**{target}** ha sido ascendido a civil sin ficha. felicitaciones supongo.",
        "nadie preguntó, pero igual dedeamos a **{target}**.",
        "**{target}**, no es personal. bueno sí, un poco.",
    ),
    creator_defense_titles=(
        "🛡️ El creador no se toca",
        "🔁 Dedeo reflejado",
        "👑 Intento rechazado",
        "🪞 Mira quién terminó dedeado",
        "⚡ Trampa activada",
        "🚫 Acceso denegado, payaso",
        "☝️ El dedo apuntó de vuelta",
        "🤡 Jugada brillante",
    ),
    creator_defense_messages=(
        "**{target}** intentó dedear al creador. JAJA. el dedo rebotó, dedeate tú nomás.",
        "bonito intento **{target}**, pero al creador no se toca. cagaste tú.",
        "**{target}** quiso hacerse el vivo con el creador y terminó dedeándose solo. aweonao.",
        "el sistema protegió al creador y sacrificó a **{target}**. gracias por participar.",
        "**{target}**, apuntaste el dedo pa arriba y te cayó en la frente. impresionante.",
        "protección divina activada. el creador sigue intacto y **{target}** queda dedeado.",
        "había todo un servidor para elegir y **{target}** fue por el creador. selección natural.",
        "el creador tiene plot armor, **{target}**. ahora entrega tu ficha por gil.",
    ),
    colors=(0xED4245, 0xF47FFF, 0xFEE75C, 0x9B59B6, 0xE67E22, 0x5865F2),
    unavailable="el dedo no funciona por MD po. usa el comando dentro del server.",
    empty="no encontré a nadie pa dedear. impresionante, hasta pa esta wea quedé solo.",
    bot_target="a los bots no se les dedea, genio. elige a una persona de carne y píxeles.",
    creator_self_target="ni tú puedes dedear al creador, aunque seas tú mismo. elige a otro, wn.",
    cooldown="ya tiraste el dedo, animal. espera **{seconds} s** antes de webear de nuevo.",
)


WALTER_DEDEO_COPY = DedeoCopy(
    titles=(
        "☝️ Dedeo oficial",
        "🎯 Selección completada",
        "🚨 Dedeo confirmado",
        "📋 Ficha seleccionada",
        "🎲 Resultado del sorteo",
        "📢 Resolución administrativa",
        "🫵 Persona seleccionada",
        "🛑 Veredicto final",
    ),
    messages=(
        "Tras una exhaustiva investigación de aproximadamente cero segundos, **{target}** ha sido dedeado.",
        "El procedimiento aleatorio seleccionó a **{target}**. No se aceptarán reclamos.",
        "**{target}** deberá presentarse inmediatamente a entregar una ficha imaginaria.",
        "El dedo institucional ha señalado a **{target}**. Que conste en acta.",
        "Por razones completamente arbitrarias, **{target}** queda oficialmente dedeado.",
        "La suerte fue consultada y decidió abandonar a **{target}**.",
        "**{target}** ha sido elegido democráticamente por un sistema que no admite votos.",
        "Se revisaron los antecedentes inexistentes de **{target}** y el resultado fue dedeo.",
    ),
    creator_defense_titles=(
        "🛡️ Protección del creador",
        "🔁 Dedeo reflejado",
        "🚫 Objetivo protegido",
        "🪞 Selección devuelta",
    ),
    creator_defense_messages=(
        "**{target}** intentó seleccionar al creador. La protección devolvió el dedeo al remitente.",
        "El creador está protegido; por tanto, **{target}** ocupa ahora su lugar en el resultado.",
        "La selección de **{target}** apuntó al creador y fue reflejada automáticamente.",
        "El sistema rechazó el objetivo protegido y dedeó a **{target}** en su lugar.",
    ),
    colors=(0xED4245, 0xFEE75C, 0x5865F2, 0x9B59B6),
    unavailable="El comando solo puede utilizarse dentro de un servidor.",
    empty="No encontré miembros disponibles para realizar el sorteo en este canal.",
    bot_target="Los bots no participan en el dedeo. Selecciona a un miembro humano.",
    creator_self_target="El creador también está protegido de sus propias selecciones.",
    cooldown="El dedo necesita descanso. Espera **{seconds} s** antes de volver a usarlo.",
)


class DedeoCog(commands.Cog):
    """Select one locally cached human member visible in the current channel."""

    def __init__(self, persona: BotPersona, provider: DatabaseConfigProvider) -> None:
        self.provider = provider
        self.persona = persona
        self.copy = _copy_for(persona.id)
        self.cooldown = DedeoCooldown()

    @discord.slash_command(
        name="dedear",
        description="Dedea a alguien o deja que el azar decida",
    )
    @discord.option(  # type: ignore[no-untyped-call,untyped-decorator]
        "usuario",
        discord.Member,
        description="Miembro a dedear; déjalo vacío para elegir al azar",
        required=False,
    )
    async def dede(
        self,
        ctx: discord.ApplicationContext,
        usuario: discord.Member | None = None,
    ) -> None:
        if (
            ctx.guild is None
            or not isinstance(ctx.author, discord.Member)
            or not isinstance(ctx.channel, (discord.TextChannel, discord.Thread))
        ):
            await ctx.respond(
                embed=_alert(self.copy.unavailable),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        bundle = await self.provider.cached(ctx.guild.id)
        options = bundle.settings.dedeo_options
        if options is None:
            await ctx.respond("El dedeo no está habilitado en este servidor.", ephemeral=True)
            return
        guild_id = ctx.guild.id
        channel_id = ctx.channel.id
        requester_id = ctx.author.id
        try:
            if usuario is not None and usuario.bot:
                await ctx.respond(
                    embed=_alert(self.copy.bot_target),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            candidates = (
                ()
                if usuario is not None
                else eligible_channel_members(ctx.guild.members, ctx.channel)
            )
            selection = select_dedeo_target(
                candidates,
                author=ctx.author,
                requested_target=usuario,
                protected_user_ids=frozenset(options.protected_user_ids),
            )
            if selection.creator_self_target:
                await ctx.respond(
                    embed=_alert(self.copy.creator_self_target),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            target = selection.target
            if target is None:
                await ctx.respond(
                    embed=_alert(self.copy.empty),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            retry_after = self.cooldown.acquire(
                guild_id=guild_id,
                user_id=requester_id,
            )
            if retry_after is not None:
                await ctx.respond(
                    embed=_cooldown_alert(self.copy.cooldown.format(seconds=retry_after)),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            await ctx.respond(
                content=f"☝️ {target.mention}",
                embed=build_dedeo_embed(
                    self.copy,
                    target_name=target.display_name,
                    target_avatar_url=str(target.display_avatar.url),
                    creator_defense=selection.creator_defense,
                ),
                ephemeral=False,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    users=[target],
                    roles=False,
                    replied_user=False,
                ),
            )
            log_event(
                LOGGER,
                "info",
                "dedeo_published",
                guild_id=guild_id,
                channel_id=channel_id,
                requester_user_id=requester_id,
                target_user_id=target.id,
                creator_defense=selection.creator_defense,
            )
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "dedeo_command_failed",
                guild_id=guild_id,
                channel_id=channel_id,
                requester_user_id=requester_id,
                exc_info=error,
                **exception_fields(error),
            )
            await ctx.respond(
                embed=_alert("se rompió hasta el dedo. prueba de nuevo más rato."),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )


def eligible_channel_members(
    members: Sequence[discord.Member],
    channel: discord.TextChannel | discord.Thread,
) -> tuple[discord.Member, ...]:
    """Return locally cached human members who can currently view the channel."""

    return tuple(
        member
        for member in members
        if not member.bot and not member.pending and channel.permissions_for(member).view_channel
    )


def select_dedeo_target(
    candidates: Sequence[discord.Member],
    *,
    author: discord.Member,
    requested_target: discord.Member | None = None,
    protected_user_ids: frozenset[int] = frozenset(),
) -> DedeoSelection:
    """Resolve explicit, reflected, or random selection without targeting the creator."""

    if requested_target is not None:
        if requested_target.id in protected_user_ids:
            if author.id in protected_user_ids:
                return DedeoSelection(target=None, creator_self_target=True)
            return DedeoSelection(target=author, creator_defense=True)
        return DedeoSelection(target=requested_target)

    unprotected = tuple(member for member in candidates if member.id not in protected_user_ids)
    others = tuple(member for member in unprotected if member.id != author.id)
    selectable = others or unprotected
    target = secrets.choice(selectable) if selectable else None
    return DedeoSelection(target=target)


def build_dedeo_embed(
    copy: DedeoCopy,
    *,
    target_name: str,
    target_avatar_url: str,
    creator_defense: bool = False,
) -> discord.Embed:
    """Render one colorful result using only local, prevalidated copy."""

    safe_target = _safe_name(target_name)
    titles = copy.creator_defense_titles if creator_defense else copy.titles
    messages = copy.creator_defense_messages if creator_defense else copy.messages
    embed = discord.Embed(
        title=secrets.choice(titles),
        description=secrets.choice(messages).format(target=safe_target),
        color=discord.Color(secrets.choice(copy.colors)),
    )
    embed.set_thumbnail(url=target_avatar_url)
    return embed


def _copy_for(persona_id: str) -> DedeoCopy:
    return ROY_DEDEO_COPY if persona_id == "roy_velazco" else WALTER_DEDEO_COPY


def _safe_name(value: str) -> str:
    return discord.utils.escape_markdown(discord.utils.escape_mentions(value.strip()))


def _alert(description: str) -> discord.Embed:
    return discord.Embed(
        title="⚠️ Dedeo no disponible",
        description=description,
        color=discord.Color.orange(),
    )


def _cooldown_alert(description: str) -> discord.Embed:
    return discord.Embed(
        title="⏳ Aguanta un poco",
        description=description,
        color=discord.Color.orange(),
    )
