"""Restricted Discord workflow for deterministic arrest-charge statistics."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date

import discord
from discord.ext import commands

from arrest_report_agent.application.date_ranges import DateRangePeriod, resolve_date_range
from arrest_report_agent.application.ports import ArrestReportLookup
from arrest_report_agent.config import BotPersona
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.discord.forms import (
    build_date_period_select,
    build_publication_checkbox,
    selected_date_period,
)
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider

from .models import (
    ArrestStatisticsResult,
    ArrestStatisticsTooLargeError,
    PreparedArrestStatistics,
)
from .service import ArrestStatisticsService

LOGGER = logging.getLogger(__name__)
MODAL_TIMEOUT_SECONDS = 300.0
VIEW_TIMEOUT_SECONDS = 300.0
PERIODS = (
    DateRangePeriod.LAST_1_DAY,
    DateRangePeriod.LAST_2_DAYS,
    DateRangePeriod.LAST_7_DAYS,
    DateRangePeriod.LAST_14_DAYS,
    DateRangePeriod.LAST_30_DAYS,
    DateRangePeriod.CUSTOM,
)


@dataclass(frozen=True, slots=True)
class _InterfaceCopy:
    unauthorized: str
    unavailable: str
    invalid_range: str
    busy: str
    failed: str


class ArrestStatisticsCog(commands.Cog):
    """Prepare and publish a bounded charge-frequency analysis."""

    def __init__(
        self,
        provider: DatabaseConfigProvider,
        service: ArrestStatisticsService,
        persona: BotPersona,
    ) -> None:
        self.provider = provider
        self.service = service
        self.default_persona = persona
        self._guild_locks: dict[int, asyncio.Lock] = {}

    @discord.slash_command(
        name="estadisticas-arrestos",
        description="Analiza los cargos registrados en reportes de arresto",
    )
    async def arrest_statistics(self, ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            await ctx.respond(
                embed=_alert("⚠️ Consulta no disponible", "Este comando solo funciona en un servidor."),
                ephemeral=True,
            )
            return
        try:
            bundle = await self.provider.cached(ctx.guild.id)
            copy = _copy(bundle.persona.profile)
            if not _member_has_any_role(
                ctx.author,
                bundle.settings.faction_report_role_ids,
            ):
                await ctx.respond(
                    embed=_alert("⚠️ Acceso restringido", copy.unauthorized),
                    ephemeral=True,
                )
                return
            if (
                bundle.settings.arrest_statistics_options is None
                or bundle.service.arrest_report_lookup is None
            ):
                await ctx.respond(
                    embed=_alert("⚠️ Consulta no disponible", copy.unavailable),
                    ephemeral=True,
                )
                return
            await ctx.send_modal(ArrestStatisticsModal(self, requester_id=ctx.author.id))
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "arrest_statistics_command_failed",
                guild_id=ctx.guild.id,
                user_id=ctx.author.id,
                exc_info=error,
                **exception_fields(error),
            )
            await ctx.respond(
                embed=_alert("⚠️ Consulta no disponible", _copy(self.default_persona).failed),
                ephemeral=True,
            )

    async def prepare(
        self,
        interaction: discord.Interaction,
        *,
        requester_id: int,
        period: DateRangePeriod,
        raw_date_from: str,
        raw_date_to: str,
        top: int,
        publish: bool,
    ) -> None:
        if not _valid_requester(interaction, requester_id):
            await interaction.response.send_message(
                embed=_alert("⚠️ Consulta no disponible", "Este formulario ya no es válido."),
                ephemeral=True,
            )
            return
        assert interaction.guild is not None
        assert isinstance(interaction.user, discord.Member)
        bundle = await self.provider.cached(interaction.guild.id)
        copy = _copy(bundle.persona.profile)
        if not _member_has_any_role(
            interaction.user,
            bundle.settings.faction_report_role_ids,
        ):
            await interaction.response.send_message(
                embed=_alert("⚠️ Acceso restringido", copy.unauthorized),
                ephemeral=True,
            )
            return
        options = bundle.settings.arrest_statistics_options
        lookup: ArrestReportLookup | None = bundle.service.arrest_report_lookup
        if options is None or lookup is None:
            await interaction.response.send_message(
                embed=_alert("⚠️ Consulta no disponible", copy.unavailable),
                ephemeral=True,
            )
            return
        try:
            date_from, date_to = resolve_date_range(
                period,
                raw_date_from,
                raw_date_to,
                max_days=options.max_range_days,
            )
        except ValueError:
            await interaction.response.send_message(
                embed=_alert("⚠️ Rango inválido", copy.invalid_range),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            prepared = await self.service.prepare(
                lookup,
                options,
                date_from=date_from,
                date_to=date_to,
            )
            view = ArrestStatisticsConfirmationView(
                self,
                requester_id=requester_id,
                prepared=prepared,
                top=min(top, options.max_top),
                publish=publish,
            )
            await interaction.followup.send(
                embed=build_confirmation_embed(prepared, top=view.top, publish=publish),
                view=view,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log_event(
                LOGGER,
                "info",
                "arrest_statistics_prepared",
                guild_id=interaction.guild.id,
                user_id=requester_id,
                date_from=date_from.isoformat(),
                date_to=date_to.isoformat(),
                reports=prepared.report_count,
                cached_details=prepared.cached_detail_count,
                estimated_requests=prepared.estimated_requests,
            )
        except ArrestStatisticsTooLargeError as error:
            reconfigure_view = ArrestStatisticsReconfigureView(
                self,
                requester_id=requester_id,
            )
            await interaction.followup.send(
                embed=build_too_large_embed(error),
                view=reconfigure_view,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "arrest_statistics_prepare_failed",
                guild_id=interaction.guild.id,
                user_id=requester_id,
                date_from=date_from.isoformat(),
                date_to=date_to.isoformat(),
                exc_info=error,
                **exception_fields(error),
            )
            await interaction.followup.send(
                embed=_alert("⚠️ No pude preparar el análisis", copy.failed),
                ephemeral=True,
            )

    async def run(
        self,
        interaction: discord.Interaction,
        *,
        requester_id: int,
        prepared: PreparedArrestStatistics,
        top: int,
        publish: bool,
    ) -> None:
        if not _valid_requester(interaction, requester_id):
            await interaction.response.send_message(
                embed=_alert("⚠️ Consulta no disponible", "Esta confirmación no te pertenece."),
                ephemeral=True,
            )
            return
        assert interaction.guild is not None
        assert isinstance(interaction.user, discord.Member)
        bundle = await self.provider.cached(interaction.guild.id)
        copy = _copy(bundle.persona.profile)
        if not _member_has_any_role(
            interaction.user,
            bundle.settings.faction_report_role_ids,
        ):
            await interaction.response.send_message(
                embed=_alert("⚠️ Acceso restringido", copy.unauthorized),
                ephemeral=True,
            )
            return
        options = bundle.settings.arrest_statistics_options
        lookup: ArrestReportLookup | None = bundle.service.arrest_report_lookup
        if options is None or lookup is None:
            await interaction.response.send_message(
                embed=_alert("⚠️ Consulta no disponible", copy.unavailable),
                ephemeral=True,
            )
            return
        lock = self._guild_locks.setdefault(interaction.guild.id, asyncio.Lock())
        if lock.locked():
            await interaction.response.send_message(
                embed=_alert("⏳ Análisis en curso", copy.busy),
                ephemeral=True,
            )
            return
        await lock.acquire()
        try:
            await interaction.response.defer()
            result = await self.service.analyze(lookup, options, prepared)
            embed = build_arrest_statistics_embed(result, top=top)
            if publish:
                if interaction.channel is None:
                    raise RuntimeError("confirmation interaction has no channel")
                await interaction.channel.send(
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                if interaction.message is not None:
                    await interaction.message.delete()
            else:
                await interaction.edit_original_response(embed=embed, view=None)
            log_event(
                LOGGER,
                "info",
                "arrest_statistics_published",
                guild_id=interaction.guild.id,
                user_id=requester_id,
                visibility="public" if publish else "private",
                date_from=result.date_from.isoformat(),
                date_to=result.date_to.isoformat(),
                reports=result.total_reports,
                analyzed_reports=result.analyzed_reports,
                charges=result.total_charges,
            )
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "arrest_statistics_analysis_failed",
                guild_id=interaction.guild.id,
                user_id=requester_id,
                exc_info=error,
                **exception_fields(error),
            )
            await interaction.edit_original_response(
                embed=_alert("⚠️ No pude completar el análisis", copy.failed),
                view=None,
            )
        finally:
            lock.release()


class ArrestStatisticsModal(discord.ui.DesignerModal):
    """Collect bounded dates, top size and public/private visibility."""

    def __init__(self, cog: ArrestStatisticsCog, requester_id: int) -> None:
        self.cog = cog
        self.requester_id = requester_id
        self.period = build_date_period_select(
            "arrest_statistics:v1:period",
            periods=PERIODS,
            default=DateRangePeriod.LAST_7_DAYS,
        )
        self.date_from = discord.ui.InputText(
            custom_id="arrest_statistics:v1:date_from",
            placeholder="AAAA-MM-DD",
            required=False,
            min_length=10,
            max_length=10,
        )
        self.date_to = discord.ui.InputText(
            custom_id="arrest_statistics:v1:date_to",
            placeholder="AAAA-MM-DD",
            required=False,
            min_length=10,
            max_length=10,
        )
        self.top = discord.ui.StringSelect(  # type: ignore[type-var]
            custom_id="arrest_statistics:v1:top",
            options=[
                discord.SelectOption(label="Top 5", value="5"),
                discord.SelectOption(label="Top 10", value="10", default=True),
            ],
            min_values=1,
            max_values=1,
        )
        self.publish = build_publication_checkbox(
            "arrest_statistics:v1:publish",
            default=True,
        )
        super().__init__(
            discord.ui.Label("Período", self.period),
            discord.ui.Label("Desde · solo para rango personalizado", self.date_from),
            discord.ui.Label("Hasta · solo para rango personalizado", self.date_to),
            discord.ui.Label("Cantidad de cargos a mostrar", self.top),
            discord.ui.Label("Publicar el análisis en este canal", self.publish),
            title="Analizar reportes de arresto",
            custom_id=f"arrest_statistics:v1:prepare:{requester_id}",
            timeout=MODAL_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.prepare(
            interaction,
            requester_id=self.requester_id,
            period=selected_date_period(
                self.period,
                default=DateRangePeriod.LAST_7_DAYS,
            ),
            raw_date_from=self.date_from.value or "",
            raw_date_to=self.date_to.value or "",
            top=int(next(iter(self.top.values or ()), "10")),
            publish=bool(self.publish.value),
        )


class ArrestStatisticsConfirmationView(discord.ui.View):
    def __init__(
        self,
        cog: ArrestStatisticsCog,
        *,
        requester_id: int,
        prepared: PreparedArrestStatistics,
        top: int,
        publish: bool,
    ) -> None:
        super().__init__(timeout=VIEW_TIMEOUT_SECONDS, disable_on_timeout=True)
        self.cog = cog
        self.requester_id = requester_id
        self.prepared = prepared
        self.top = top
        self.publish = publish

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user is not None and interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            embed=_alert("⚠️ Confirmación restringida", "Solo quien abrió el análisis puede iniciarlo."),
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Analizar", emoji="📊", style=discord.ButtonStyle.primary)
    async def analyze_button(
        self,
        _button: discord.ui.Button[ArrestStatisticsConfirmationView],
        interaction: discord.Interaction,
    ) -> None:
        await self.cog.run(
            interaction,
            requester_id=self.requester_id,
            prepared=self.prepared,
            top=self.top,
            publish=self.publish,
        )

    @discord.ui.button(label="Cancelar", emoji="✖️", style=discord.ButtonStyle.secondary)
    async def cancel_button(
        self,
        _button: discord.ui.Button[ArrestStatisticsConfirmationView],
        interaction: discord.Interaction,
    ) -> None:
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Análisis cancelado",
                description="No se consultaron los detalles de los reportes.",
                color=discord.Color.dark_grey(),
            ),
            view=None,
        )


class ArrestStatisticsReconfigureView(discord.ui.View):
    def __init__(self, cog: ArrestStatisticsCog, *, requester_id: int) -> None:
        super().__init__(timeout=VIEW_TIMEOUT_SECONDS, disable_on_timeout=True)
        self.cog = cog
        self.requester_id = requester_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user is not None and interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            embed=_alert("⚠️ Acción restringida", "Solo quien abrió la consulta puede cambiarla."),
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Volver a configurar", emoji="↩️", style=discord.ButtonStyle.secondary)
    async def reconfigure_button(
        self,
        _button: discord.ui.Button[ArrestStatisticsReconfigureView],
        interaction: discord.Interaction,
    ) -> None:
        await interaction.response.send_modal(
            ArrestStatisticsModal(self.cog, requester_id=self.requester_id)
        )


def build_confirmation_embed(
    prepared: PreparedArrestStatistics,
    *,
    top: int,
    publish: bool,
) -> discord.Embed:
    return discord.Embed(
        title="📊 Confirmar análisis de arrestos",
        description=(
            f"**Período:** {_format_date(prepared.date_from)} — {_format_date(prepared.date_to)}\n"
            f"**Reportes encontrados:** {prepared.report_count}\n"
            f"**Detalles en caché:** {prepared.cached_detail_count}\n"
            f"**Consultas adicionales estimadas:** {prepared.estimated_requests}\n"
            f"**Resultado:** Top {top} · "
            f"{'Visible para todos' if publish else 'Solo para ti'}\n\n"
            "Confirma para consultar los detalles y preparar el resumen."
        ),
        color=discord.Color.gold(),
    )


def build_too_large_embed(error: ArrestStatisticsTooLargeError) -> discord.Embed:
    return discord.Embed(
        title="⚠️ Rango demasiado extenso",
        description=(
            f"El período contiene **{error.count} reportes** y el límite seguro es "
            f"**{error.limit}**. Reduce el rango para no saturar la consulta."
        ),
        color=discord.Color.orange(),
    )


def build_arrest_statistics_embed(
    result: ArrestStatisticsResult,
    *,
    top: int,
) -> discord.Embed:
    coverage = (
        f"{result.analyzed_reports} de {result.total_reports} reportes analizados"
        if result.failed_reports
        else f"{result.analyzed_reports} reportes analizados"
    )
    embed = discord.Embed(
        title="📊 Tendencias de arrestos",
        description=(
            f"**Período:** {_format_date(result.date_from)} — {_format_date(result.date_to)}\n"
            f"**Cobertura:** {coverage} · **Cargos:** {result.total_charges}"
        ),
        color=discord.Color.dark_blue(),
    )
    charge_lines = [
        (
            f"**{position}.** {_charge_label(item.name, item.code)} · "
            f"**{item.count}** · {_percentage(item.percentage)}"
        )
        for position, item in enumerate(result.top_charges[:top], start=1)
    ]
    embed.add_field(
        name="⚖️ Cargos más frecuentes",
        value=_fit_lines(charge_lines) if charge_lines else "No se registraron cargos en el período.",
        inline=False,
    )
    combination_lines = [
        (
            " + ".join(_charge_label(name, code) for name, code in item.charges)
            + f" · **{item.report_count} reportes**"
        )
        for item in result.top_combinations[:5]
    ]
    if combination_lines:
        embed.add_field(
            name="🔗 Combinaciones frecuentes",
            value=_fit_lines(combination_lines),
            inline=False,
        )
    total = result.total_charges
    classification = [
        f"**Menores:** {result.minor_charges} · {_percentage_of(result.minor_charges, total)}",
        f"**Graves:** {result.serious_charges} · {_percentage_of(result.serious_charges, total)}",
    ]
    if result.unknown_charges:
        classification.append(
            f"**Sin clasificar:** {result.unknown_charges} · "
            f"{_percentage_of(result.unknown_charges, total)}"
        )
    embed.add_field(
        name="🚨 Clasificación",
        value=" | ".join(classification),
        inline=False,
    )
    if result.failed_reports:
        embed.set_footer(
            text=f"{result.failed_reports} reportes no pudieron consultarse y fueron omitidos."
        )
    return embed


def _copy(persona: BotPersona) -> _InterfaceCopy:
    if persona.id == "roy_velazco":
        return _InterfaceCopy(
            unauthorized="Esto es pa command y executive, no pa cualquiera q quiera sapear números.",
            unavailable="No dejaron habilitadas las estadísticas de arrestos en este servidor.",
            invalid_range="Revisa las fechas. Usa AAAA-MM-DD y no te mandís un rango eterno.",
            busy="Ya hay un análisis andando. Espera un poco, no revienten la API por ansiedad.",
            failed="No pude armar las estadísticas ahora. Prueba otra vez más rato.",
        )
    return _InterfaceCopy(
        unauthorized="Esta consulta está reservada al personal de mando y dirección autorizado.",
        unavailable="Las estadísticas de arrestos no están habilitadas para este servidor.",
        invalid_range="Revise las fechas, el formato AAAA-MM-DD y el límite permitido.",
        busy="Ya existe un análisis en curso para este servidor. Inténtelo nuevamente en breve.",
        failed="No pude completar la consulta en este momento. Inténtelo nuevamente más tarde.",
    )


def _valid_requester(interaction: discord.Interaction, requester_id: int) -> bool:
    return (
        interaction.guild is not None
        and interaction.channel is not None
        and isinstance(interaction.user, discord.Member)
        and interaction.user.id == requester_id
    )


def _member_has_any_role(member: discord.Member, role_ids: tuple[int, ...]) -> bool:
    allowed = set(role_ids)
    return bool(allowed and any(role.id in allowed for role in member.roles))


def _charge_label(name: str, code: str | None) -> str:
    escaped_name = discord.utils.escape_markdown(name)
    return f"{escaped_name} ({discord.utils.escape_markdown(code)})" if code else escaped_name


def _fit_lines(lines: list[str], *, limit: int = 1_024) -> str:
    output: list[str] = []
    used = 0
    for line in lines:
        compact = line if len(line) <= 300 else f"{line[:297]}…"
        extra = len(compact) + (1 if output else 0)
        if used + extra > limit:
            break
        output.append(compact)
        used += extra
    return "\n".join(output) or "—"


def _percentage(value: float) -> str:
    return f"{value:.1f}%".replace(".", ",")


def _percentage_of(value: int, total: int) -> str:
    return _percentage(value * 100 / total if total else 0.0)


def _format_date(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def _alert(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=discord.Color.red())
