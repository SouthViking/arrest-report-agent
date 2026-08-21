"""Discord command and compact presentation for stolen vehicles."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from enum import StrEnum
from math import ceil

import discord
from discord.ext import commands

from arrest_report_agent.config import BotPersona, PersonaStolenVehicleMessages
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider
from arrest_report_agent.infrastructure.sarp import SarpStolenVehicle

from .models import StolenVehicleQueryResult, StolenVehicleStatusFilter
from .service import StolenVehicleService

LOGGER = logging.getLogger(__name__)
MODAL_TIMEOUT_SECONDS = 300
VIEW_TIMEOUT_SECONDS = 300
ITEMS_PER_PAGE = 10


class StolenVehicleVisibility(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"


class StolenVehicleCog(commands.Cog):
    """Expose recent vehicle-theft reports to configured operational staff."""

    def __init__(
        self,
        provider: DatabaseConfigProvider,
        service: StolenVehicleService,
        persona: BotPersona,
    ) -> None:
        self.provider = provider
        self.service = service
        self.default_persona = persona
        self._guild_locks: dict[int, asyncio.Lock] = {}
        self.stolen_vehicles.description = (
            persona.messages.interface.commands.stolen_vehicles_description
        )

    @discord.slash_command(
        name="vehiculos-robados",
        description="Muestra las denuncias recientes de vehículos robados",
    )
    async def stolen_vehicles(self, ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            await ctx.respond(
                embed=_generic_alert("Este comando solo funciona dentro de un servidor."),
                ephemeral=True,
            )
            return
        try:
            bundle = await self.provider.cached(ctx.guild.id)
            ui = bundle.persona.profile.messages.interface.stolen_vehicles
            if not _member_has_any_role(
                ctx.author,
                bundle.settings.operational_staff_role_ids,
            ):
                await ctx.respond(embed=_alert(ui.alert_title, ui.unauthorized), ephemeral=True)
                return
            if not bundle.settings.stolen_vehicles_enabled:
                await ctx.respond(embed=_alert(ui.alert_title, ui.unavailable), ephemeral=True)
                return
            await ctx.send_modal(StolenVehicleSearchModal(self, ctx.author.id))
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "stolen_vehicle_command_failed",
                guild_id=ctx.guild.id,
                user_id=ctx.author.id,
                exc_info=error,
                **exception_fields(error),
            )
            ui = self.default_persona.messages.interface.stolen_vehicles
            await ctx.respond(embed=_alert(ui.alert_title, ui.operation_failed), ephemeral=True)

    async def search(
        self,
        interaction: discord.Interaction,
        *,
        requester_id: int,
        raw_count: str,
        status: StolenVehicleStatusFilter,
        visibility: StolenVehicleVisibility,
    ) -> None:
        if (
            interaction.guild is None
            or interaction.channel is None
            or not isinstance(interaction.user, discord.Member)
            or interaction.user.id != requester_id
        ):
            await interaction.response.send_message(
                embed=_generic_alert("La consulta ya no es válida."),
                ephemeral=True,
            )
            return
        bundle = await self.provider.cached(interaction.guild.id)
        ui = bundle.persona.profile.messages.interface.stolen_vehicles
        if not _member_has_any_role(
            interaction.user,
            bundle.settings.operational_staff_role_ids,
        ):
            await interaction.response.send_message(
                embed=_alert(ui.alert_title, ui.unauthorized),
                ephemeral=True,
            )
            return
        if not bundle.settings.stolen_vehicles_enabled:
            await interaction.response.send_message(
                embed=_alert(ui.alert_title, ui.unavailable),
                ephemeral=True,
            )
            return
        try:
            count = int(raw_count.strip())
            if not 1 <= count <= 50:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                embed=_alert(ui.alert_title, ui.invalid_count),
                ephemeral=True,
            )
            return

        private = visibility is StolenVehicleVisibility.PRIVATE
        lock = self._guild_locks.setdefault(interaction.guild.id, asyncio.Lock())
        if lock.locked():
            await interaction.response.send_message(
                embed=_alert(ui.alert_title, ui.busy),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=private)
        try:
            async with lock:
                result = await self.service.list_recent(count=count, status=status)
            view = StolenVehiclePaginationView(
                requester_id=requester_id,
                result=result,
                ui=ui,
            )
            if view.page_count > 1:
                await interaction.followup.send(
                    embed=view.current_embed,
                    view=view,
                    ephemeral=private,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await interaction.followup.send(
                    embed=view.current_embed,
                    ephemeral=private,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            log_event(
                LOGGER,
                "info",
                "stolen_vehicle_list_published",
                guild_id=interaction.guild.id,
                channel_id=interaction.channel.id,
                user_id=interaction.user.id,
                visibility=visibility.value,
                status=status.value,
                requested_count=count,
                returned_count=len(result.items),
            )
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "stolen_vehicle_list_failed",
                guild_id=interaction.guild.id,
                channel_id=interaction.channel.id,
                user_id=interaction.user.id,
                exc_info=error,
                **exception_fields(error),
            )
            await interaction.followup.send(
                embed=_alert(ui.alert_title, ui.operation_failed),
                ephemeral=private,
                allowed_mentions=discord.AllowedMentions.none(),
            )


class StolenVehicleSearchModal(discord.ui.DesignerModal):
    def __init__(self, cog: StolenVehicleCog, requester_id: int) -> None:
        self.cog = cog
        self.requester_id = requester_id
        self.count = discord.ui.InputText(
            custom_id="stolen_vehicles:v1:count",
            value="10",
            min_length=1,
            max_length=2,
        )
        self.status = discord.ui.StringSelect(  # type: ignore[type-var]
            custom_id="stolen_vehicles:v1:status",
            options=[
                discord.SelectOption(label="Activos", value="active", emoji="🔴", default=True),
                discord.SelectOption(label="Resueltos", value="resolved", emoji="⚪"),
                discord.SelectOption(label="Todos", value="all", emoji="🚘"),
            ],
            min_values=1,
            max_values=1,
        )
        self.visibility = discord.ui.StringSelect(  # type: ignore[type-var]
            custom_id="stolen_vehicles:v1:visibility",
            options=[
                discord.SelectOption(
                    label="Visible para todos",
                    value="public",
                    emoji="🌐",
                    default=True,
                ),
                discord.SelectOption(label="Solo para mí", value="private", emoji="🔒"),
            ],
            min_values=1,
            max_values=1,
        )
        super().__init__(
            discord.ui.Label("Cantidad · entre 1 y 50", self.count),
            discord.ui.Label("Estado", self.status),
            discord.ui.Label("Quién puede ver el resultado", self.visibility),
            title="Consultar vehículos robados",
            custom_id=f"stolen_vehicles:v1:search:{requester_id}",
            timeout=MODAL_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.search(
            interaction,
            requester_id=self.requester_id,
            raw_count=self.count.value or "10",
            status=StolenVehicleStatusFilter(
                next(iter(self.status.values or ()), StolenVehicleStatusFilter.ACTIVE.value)
            ),
            visibility=StolenVehicleVisibility(
                next(iter(self.visibility.values or ()), StolenVehicleVisibility.PUBLIC.value)
            ),
        )


class StolenVehiclePaginationView(discord.ui.View):
    def __init__(
        self,
        *,
        requester_id: int,
        result: StolenVehicleQueryResult,
        ui: PersonaStolenVehicleMessages,
    ) -> None:
        super().__init__(timeout=VIEW_TIMEOUT_SECONDS, disable_on_timeout=True)
        self.requester_id = requester_id
        self.result = result
        self.ui = ui
        self.page = 0
        self._synchronize_buttons()

    @property
    def page_count(self) -> int:
        return max(1, ceil(len(self.result.items) / ITEMS_PER_PAGE))

    @property
    def current_embed(self) -> discord.Embed:
        return build_stolen_vehicle_list_embed(
            self.result,
            requester_id=self.requester_id,
            page=self.page,
            ui=self.ui,
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user is not None and interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            embed=_alert(self.ui.alert_title, self.ui.other_query),
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Anterior", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous_page(
        self,
        _button: discord.ui.Button[StolenVehiclePaginationView],
        interaction: discord.Interaction,
    ) -> None:
        self.page = max(0, self.page - 1)
        self._synchronize_buttons()
        await interaction.response.edit_message(embed=self.current_embed, view=self)

    @discord.ui.button(label="Siguiente", emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next_page(
        self,
        _button: discord.ui.Button[StolenVehiclePaginationView],
        interaction: discord.Interaction,
    ) -> None:
        self.page = min(self.page_count - 1, self.page + 1)
        self._synchronize_buttons()
        await interaction.response.edit_message(embed=self.current_embed, view=self)

    def _synchronize_buttons(self) -> None:
        self.previous_page.disabled = self.page <= 0
        self.next_page.disabled = self.page >= self.page_count - 1


def build_stolen_vehicle_list_embed(
    result: StolenVehicleQueryResult,
    *,
    requester_id: int,
    page: int,
    ui: PersonaStolenVehicleMessages,
) -> discord.Embed:
    page_count = max(1, ceil(len(result.items) / ITEMS_PER_PAGE))
    bounded_page = min(max(page, 0), page_count - 1)
    start = bounded_page * ITEMS_PER_PAGE
    items = result.items[start : start + ITEMS_PER_PAGE]
    lines = [
        f"**Mostrando:** {len(result.items)} más recientes · {result.status.label}",
        "**Estado:** 🔴 Activo · ⚪ Resuelto",
    ]
    if items:
        lines.append(
            "\n".join(_compact_vehicle_line(item) for item in items)
        )
    else:
        lines.append(ui.no_results)
    lines.append(f"*Consulta de <@{requester_id}>*.")
    embed = discord.Embed(
        title="🚘 Vehículos robados",
        description="\n\n".join(lines),
        color=(
            discord.Color(0xE67E22)
            if any(item.is_active for item in result.items)
            else discord.Color.light_grey()
        ),
        timestamp=datetime.now(UTC),
    )
    if page_count > 1:
        embed.set_footer(text=f"Página {bounded_page + 1} de {page_count}")
    return embed


def build_stolen_vehicle_alert_embed(vehicle: SarpStolenVehicle) -> discord.Embed:
    embed = discord.Embed(
        title="⚠️🚘 Vehículo denunciado como robado",
        description=(
            f"**Descripción**\n{_safe(vehicle.description or 'No informada', limit=1000)}"
        ),
        color=discord.Color(0xE67E22),
        timestamp=vehicle.created_at,
    )
    embed.add_field(name="🔢 Matrícula", value=f"`{_safe_code(vehicle.plate)}`", inline=True)
    embed.add_field(name="🚘 Modelo", value=_model_label(vehicle), inline=True)
    embed.add_field(name="🎨 Colores", value=_colors_label(vehicle), inline=True)
    embed.add_field(
        name="👤 Propietario",
        value=_safe(vehicle.owner_name or "No informado", limit=100),
        inline=True,
    )
    embed.add_field(
        name="🕒 Denunciado",
        value=f"<t:{int(vehicle.created_at.timestamp())}:R>",
        inline=True,
    )
    return embed


def _compact_vehicle_line(vehicle: SarpStolenVehicle) -> str:
    state = "🔴" if vehicle.is_active else "⚪"
    timestamp = int(vehicle.created_at.timestamp())
    return (
        f"{state} `{_safe_code(vehicle.plate)}` · {_model_label(vehicle)} · "
        f"{_colors_label(vehicle)} · "
        f"{_safe(vehicle.owner_name or 'No informado', limit=80)} · <t:{timestamp}:R>"
    )


def _model_label(vehicle: SarpStolenVehicle) -> str:
    return _safe(str(vehicle.model) if vehicle.model is not None else "No informado", limit=80)


def _colors_label(vehicle: SarpStolenVehicle) -> str:
    colors = vehicle.colors
    value = "/".join(colors) if isinstance(colors, tuple) else colors or "No informados"
    return _safe(value, limit=100)


def _safe(value: str, *, limit: int) -> str:
    escaped = discord.utils.escape_mentions(discord.utils.escape_markdown(value.strip()))
    return escaped[:limit] or "No informado"


def _safe_code(value: str) -> str:
    return discord.utils.escape_mentions(value.strip().replace("`", "ˋ"))[:32] or "—"


def _member_has_any_role(member: discord.Member, role_ids: tuple[int, ...]) -> bool:
    allowed = set(role_ids)
    return bool(allowed and any(role.id in allowed for role in member.roles))


def _alert(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=discord.Color.red())


def _generic_alert(description: str) -> discord.Embed:
    return _alert("⚠️ Vehículos robados no disponibles", description)
