"""Role-restricted Discord history statistics without SARP or model calls."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

from arrest_report_agent.application.timezones import timezone_label
from arrest_report_agent.config import ServiceCallOptions
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider, GuildRuntimeBundle
from arrest_report_agent.service_calls.statistics import (
    CallCategory,
    ServiceCallStatisticsQuery,
    ServiceCallStatisticsResult,
)

from .forms import (
    ResultVisibility,
    build_timezone_select,
    build_visibility_select,
    selected_visibility,
)

LOGGER = logging.getLogger(__name__)
CALL_TITLE = re.compile(r"(?:(?P<service>.+) · )?Llamada #(?P<id>[0-9]{5,20})")
CALL_FIELDS = frozenset({"📍 Ubicación", "👤 Solicitante", "📞 Teléfono"})


def recognize_service_call(
    message: discord.Message, bot_id: int,
) -> tuple[int, CallCategory, datetime] | None:
    """Require this bot's identity and card structure, not a user-written keyword."""

    if message.author.id != bot_id or message.webhook_id is not None:
        return None
    for embed in message.embeds:
        title = CALL_TITLE.fullmatch(embed.title or "")
        if (
            title is None
            or not (embed.description or "").startswith("🚨 **Información recibida**\n")
            or not CALL_FIELDS.issubset(field.name for field in embed.fields)
        ):
            continue
        call_id = int(title["id"])
        if call_id <= 0:
            continue
        if embed.timestamp is None or embed.timestamp.tzinfo is None:
            raise ValueError("Service-call card is missing its original timestamp")
        service = title["service"] or ""
        if service in {"🚓 Policía", "Policía", "🚓 LSPD", "LSPD", "🚓 LSSD", "LSSD"}:
            category = CallCategory.POLICE
        elif service in {"🚒 LSFD", "LSFD"}:
            category = CallCategory.FIRE_MEDICAL
        else:
            category = CallCategory.OTHER
        return call_id, category, embed.timestamp.astimezone(UTC)
    return None


class DiscordServiceCallStatistics:
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

    async def count(
        self,
        guild: discord.Guild,
        member: discord.Member,
        channel_ids: tuple[int, ...],
        query: ServiceCallStatisticsQuery,
        options: ServiceCallOptions,
        *,
        now: datetime | None = None,
    ) -> ServiceCallStatisticsResult:
        bot_member = guild.me
        if bot_member is None or self.bot.user is None:
            raise ValueError("El bot todavía no está listo para consultar el historial.")
        channels: list[int] = []
        scanned = invalid = 0
        unavailable = truncated = False
        calls: dict[int, CallCategory] = {}
        # Discord filters publication time, not the original SARP timestamp inside the card.
        after = query.starts_at - timedelta(seconds=1)
        before = min(
            query.ends_at + timedelta(hours=options.statistics_late_delivery_hours),
            now or datetime.now(UTC),
        ) + timedelta(seconds=1)
        for channel_id in sorted(set(channel_ids)):
            channel = guild.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await guild.fetch_channel(channel_id)
                except (discord.Forbidden, discord.HTTPException):
                    unavailable = True
                    continue
            if not isinstance(channel, discord.TextChannel):
                unavailable = True
                continue
            # Operational roles do not bypass Discord channel/history access.
            if not _can_read(channel, member):
                continue
            if not _can_read(channel, bot_member):
                unavailable = True
                continue
            channels.append(channel.id)
            channel_scanned = 0
            try:
                async for message in channel.history(
                    after=after,
                    before=before,
                    oldest_first=True,
                    limit=options.statistics_max_messages_per_channel + 1,
                ):
                    channel_scanned += 1
                    if channel_scanned > options.statistics_max_messages_per_channel:
                        truncated = True
                        break
                    scanned += 1
                    try:
                        call = recognize_service_call(message, self.bot.user.id)
                    except ValueError:
                        invalid += 1
                        continue
                    if call is not None:
                        call_id, category, timestamp = call
                        if query.includes(timestamp):
                            calls.setdefault(call_id, category)
            except (discord.Forbidden, discord.HTTPException) as error:
                unavailable = True
                log_event(
                    LOGGER, "warning", "service_call_statistics_history_failed",
                    guild_id=guild.id, channel_id=channel.id, exc_info=error, **exception_fields(error),
                )
        if not channels:
            raise ValueError("No hay canales de dispatch cuyo historial podamos leer tú y el bot.")
        counts = Counter(calls.values())
        return ServiceCallStatisticsResult(
            query=query, police=counts[CallCategory.POLICE], fire_medical=counts[CallCategory.FIRE_MEDICAL],
            other=counts[CallCategory.OTHER], channel_ids=tuple(channels), messages_scanned=scanned,
            late_delivery_hours=options.statistics_late_delivery_hours, truncated=truncated,
            unavailable=unavailable, invalid_timestamps=invalid,
        )


def _can_read(channel: discord.TextChannel, member: discord.Member) -> bool:
    permissions = channel.permissions_for(member)
    return bool(permissions.view_channel and permissions.read_message_history)


def build_service_call_statistics_embed(result: ServiceCallStatisticsResult) -> discord.Embed:
    query = result.query
    zone = ZoneInfo(query.timezone_key)
    lines = [
        f"**{'Total parcial' if result.incomplete else 'Total'}:** {result.total} llamadas únicas\n",
        f"🚓 **Policiales:** {result.police} ({result.percentage(result.police)} %)",
        f"🚑🚒 **Ambulancia/bomberos:** {result.fire_medical} ({result.percentage(result.fire_medical)} %)",
    ]
    if result.other:
        lines.append(f"**Otros:** {result.other} ({result.percentage(result.other)} %)")
    if result.incomplete:
        lines.append("\n⚠️ **Conteo parcial:** no se pudo completar toda la lectura del historial.")
    embed = discord.Embed(
        title="📞 Llamadas de servicio",
        description="\n".join(lines),
        color=discord.Color.orange() if result.incomplete else discord.Color.blue(),
    )
    embed.add_field(name="Zona horaria", value=timezone_label(query.timezone_key), inline=False)
    embed.add_field(name="Desde", value=query.starts_at.astimezone(zone).strftime("%d/%m/%Y · %H:%M"))
    embed.add_field(name="Hasta", value=query.ends_at.astimezone(zone).strftime("%d/%m/%Y · %H:%M"))
    embed.add_field(
        name="Canales consultados",
        value=" · ".join(f"<#{channel_id}>" for channel_id in result.channel_ids[:25])
        + (f" · +{len(result.channel_ids) - 25}" if len(result.channel_ids) > 25 else ""),
        inline=False,
    )
    embed.set_footer(text=(
        "Solo incluye llamadas registradas y conservadas en Discord. "
        f"Margen para publicaciones retrasadas: {result.late_delivery_hours} h."
    ))
    return embed


class ServiceCallStatisticsCog(commands.Cog):
    def __init__(self, bot: discord.Bot, provider: DatabaseConfigProvider) -> None:
        self.provider = provider
        self.reader = DiscordServiceCallStatistics(bot)
        self._active_guilds: set[int] = set()

    async def _authorize(self, guild: discord.Guild | None, member: discord.Member) -> GuildRuntimeBundle:
        if guild is None or not isinstance(member, discord.Member):
            raise ValueError("Este comando solo funciona para miembros de un servidor.")
        bundle = await self.provider.cached(guild.id)
        allowed = set(bundle.settings.operational_staff_role_ids)
        if not any(role.id in allowed for role in member.roles):
            raise ValueError("Solo el personal de la facción autorizado puede consultar estas estadísticas.")
        if bundle.settings.service_call_options is None or not bundle.settings.service_call_channel_ids:
            raise ValueError("El dispatch no está configurado o habilitado en este servidor.")
        return bundle

    @discord.slash_command(
        name="estadisticas-llamadas",
        description="Cuenta llamadas del dispatch por fecha, hora y zona horaria",
    )
    async def service_call_statistics(self, ctx: discord.ApplicationContext) -> None:
        try:
            await self._authorize(ctx.guild, ctx.author)
            await ctx.send_modal(ServiceCallStatisticsModal(self, ctx.guild.id, ctx.author.id))
        except (ValueError, LookupError) as error:
            await ctx.respond(
                embed=_alert(str(error)), ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as error:
            self._log_failure(error, ctx.guild, ctx.author.id)
            await ctx.respond(embed=_alert("No pude abrir la consulta. Inténtalo de nuevo."), ephemeral=True)

    async def count(
        self,
        interaction: discord.Interaction,
        *,
        guild_id: int,
        requester_id: int,
        raw_from: str,
        raw_to: str,
        timezone_key: str,
        visibility: ResultVisibility,
    ) -> None:
        if (
            interaction.user.id != requester_id or interaction.guild is None
            or interaction.guild.id != guild_id
        ):
            await interaction.response.send_message(
                embed=_alert("Este formulario ya no es válido para tu usuario o servidor."),
                ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        private = visibility is ResultVisibility.PRIVATE
        await interaction.response.defer(ephemeral=private)
        acquired = False
        try:
            bundle = await self._authorize(interaction.guild, interaction.user)
            options = bundle.settings.service_call_options
            assert options is not None
            query = ServiceCallStatisticsQuery.parse(
                raw_from, raw_to, timezone_key, max_days=options.statistics_max_range_days,
            )
            if guild_id in self._active_guilds:
                raise ValueError("Ya hay una consulta en curso. Espera a que termine antes de intentar otra.")
            self._active_guilds.add(guild_id)
            acquired = True
            async with asyncio.timeout(options.statistics_timeout_seconds):
                result = await self.reader.count(
                    interaction.guild, interaction.user,
                    bundle.settings.service_call_channel_ids, query, options,
                )
            await interaction.followup.send(
                embed=build_service_call_statistics_embed(result), ephemeral=private,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log_event(
                LOGGER, "info", "service_call_statistics_published", guild_id=guild_id,
                user_id=requester_id, starts_at=query.starts_at.isoformat(),
                ends_at=query.ends_at.isoformat(),
                timezone=timezone_key, total=result.total, messages_scanned=result.messages_scanned,
                incomplete=result.incomplete, visibility=visibility.value,
            )
        except (ValueError, LookupError, TimeoutError) as error:
            message = str(error) if isinstance(error, (ValueError, LookupError)) else (
                "La consulta tardó demasiado. Prueba con un rango más pequeño."
            )
            await interaction.followup.send(
                embed=_alert(message), ephemeral=private, allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as error:
            self._log_failure(error, interaction.guild, requester_id)
            await interaction.followup.send(
                embed=_alert("No pude completar la consulta del historial. Inténtalo de nuevo."),
                ephemeral=private, allowed_mentions=discord.AllowedMentions.none(),
            )
        finally:
            if acquired:
                self._active_guilds.discard(guild_id)

    @staticmethod
    def _log_failure(error: Exception, guild: discord.Guild | None, user_id: int) -> None:
        log_event(
            LOGGER, "error", "service_call_statistics_failed", guild_id=guild.id if guild else None,
            user_id=user_id, exc_info=error, **exception_fields(error),
        )


class ServiceCallStatisticsModal(discord.ui.DesignerModal):
    def __init__(self, cog: ServiceCallStatisticsCog, guild_id: int, requester_id: int) -> None:
        self.cog = cog
        self.guild_id = guild_id
        self.requester_id = requester_id
        self.start = discord.ui.InputText(
            custom_id="service-call-statistics:v1:start", placeholder="DD/MM/AAAA HH:MM",
            min_length=16, max_length=16,
        )
        self.end = discord.ui.InputText(
            custom_id="service-call-statistics:v1:end", placeholder="DD/MM/AAAA HH:MM",
            min_length=16, max_length=16,
        )
        self.timezone = build_timezone_select("service-call-statistics:v1:timezone")
        self.visibility = build_visibility_select("service-call-statistics:v1:visibility")
        super().__init__(
            discord.ui.Label("Desde · fecha y hora local", self.start),
            discord.ui.Label("Hasta · fecha y hora local", self.end),
            discord.ui.Label("Zona horaria", self.timezone),
            discord.ui.Label("Visibilidad", self.visibility),
            title="Estadísticas de llamadas",
            custom_id=f"service-call-statistics:v1:{requester_id}", timeout=300,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            visibility = selected_visibility(self.visibility)
        except ValueError:
            await interaction.response.send_message(
                embed=_alert("Selecciona una visibilidad válida."), ephemeral=True,
            )
            return
        await self.cog.count(
            interaction, guild_id=self.guild_id, requester_id=self.requester_id,
            raw_from=self.start.value or "", raw_to=self.end.value or "",
            timezone_key=next(iter(self.timezone.values or ()), ""), visibility=visibility,
        )


def _alert(message: str) -> discord.Embed:
    return discord.Embed(title="⚠️ Estadísticas de llamadas", description=message, color=discord.Color.red())
