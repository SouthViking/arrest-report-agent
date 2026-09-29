"""Operational-staff fleet commands with private configuration and snapshot pagination."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from arrest_report_agent.application.member_names import (
    MemberSearchDirectory,
    load_member_profiles,
    unique_profile_match,
)
from arrest_report_agent.config import FleetOptions
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider

from .rendering import VIEW_LABELS, fleet_embed, fleet_info_embed, fleet_page_count, safe_text, vehicle_embed
from .service import AlprFilter, FleetService, FleetSnapshot, FleetView

LOGGER = logging.getLogger(__name__)
MODELS_PER_PAGE = 24  # Reserve the 25th option for "all models".


def alert(message: str) -> discord.Embed:
    return discord.Embed(title="🚓 Consulta de flota", description=message, color=discord.Color.orange())


class FleetCog(commands.Cog):
    def __init__(
        self,
        provider: DatabaseConfigProvider,
        service: FleetService,
        directory: MemberSearchDirectory,
    ) -> None:
        self.provider = provider
        self.service = service
        self.directory = directory

    async def authorize(self, guild_id: int, member: discord.Member) -> FleetOptions:
        bundle = await self.provider.cached(guild_id)
        if not any(role.id in bundle.settings.operational_staff_role_ids for role in member.roles):
            raise PermissionError("Esta consulta está disponible para el personal operativo de la facción.")
        options = bundle.settings.fleet_options
        if options is None:
            raise PermissionError("La consulta de flota no está habilitada en este servidor.")
        return options

    async def drivers(self, guild_id: int, snapshot: FleetSnapshot) -> dict[str, int]:
        names = {v.last_driver.strip() for v in snapshot.vehicles if v.last_driver and v.last_driver.strip()}
        if not names:
            return {}
        try:
            profiles = await load_member_profiles(self.directory, guild_id)
            matches: dict[str, int] = {}
            for name in names:
                member, _ = unique_profile_match(name, profiles)
                if member is not None:
                    matches[name] = member.user_id
            return matches
        except Exception as error:
            log_event(
                LOGGER,
                "warning",
                "fleet_driver_matching_unavailable",
                guild_id=guild_id,
                **exception_fields(error),
            )
            return {}  # Still show SARP's name; matching must not break the fleet query.

    @discord.slash_command(name="vehiculo", description="Consulta un vehículo de la facción por matrícula")
    @discord.option(  # type: ignore[no-untyped-call,untyped-decorator]
        "matricula", str, description="Matrícula real o visible del vehículo", min_length=1, max_length=32,
    )
    async def vehicle(
        self,
        ctx: discord.ApplicationContext,
        matricula: str,
    ) -> None:
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            await ctx.respond(embed=alert("Utiliza este comando dentro del servidor."), ephemeral=True)
            return
        try:
            options = await self.authorize(ctx.guild.id, ctx.author)
        except PermissionError as error:
            await ctx.respond(embed=alert(str(error)), ephemeral=True)
            return
        except Exception as error:
            await self.failed(ctx.interaction, error)
            return
        await ctx.defer(ephemeral=False)
        try:
            snapshot = await self.service.snapshot(options.faction_id, cache_seconds=options.cache_seconds)
            found = snapshot.find_plate(matricula)
            if not found:
                embed = alert("No encontré esa matrícula en la flota de la facción. Comprueba el número.")
            elif len(found) > 1:
                embed = alert("La matrícula coincide con varios vehículos. Prueba con su matrícula real.")
            else:
                drivers = await self.drivers(ctx.guild.id, snapshot)
                embed = vehicle_embed(found[0], drivers)
            await ctx.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except Exception as error:
            await self.failed(ctx.interaction, error)

    @discord.slash_command(name="flota", description="Consulta el resumen y el estado de la flota")
    async def fleet(self, ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            await ctx.respond(embed=alert("Utiliza este comando dentro del servidor."), ephemeral=True)
            return
        try:
            options = await self.authorize(ctx.guild.id, ctx.author)
        except PermissionError as error:
            await ctx.respond(embed=alert(str(error)), ephemeral=True)
            return
        except Exception as error:
            await self.failed(ctx.interaction, error)
            return
        # The model catalog itself requires SARP. A private entry panel acknowledges
        # immediately and safely handles slow API responses and >25 model names.
        await ctx.defer(ephemeral=True)
        try:
            snapshot = await self.service.snapshot(options.faction_id, cache_seconds=options.cache_seconds)
            panel = FleetModelView(self, ctx.guild.id, ctx.author.id, snapshot)
            await ctx.followup.send(
                embed=panel.embed(),
                view=panel,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as error:
            await self.failed(ctx.interaction, error)

    @discord.slash_command(name="info-flota", description="Explica cómo consultar vehículos y la flota")
    async def fleet_info(self, ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            await ctx.respond(embed=alert("Utiliza este comando dentro del servidor."), ephemeral=True)
            return
        try:
            await self.authorize(ctx.guild.id, ctx.author)
        except PermissionError as error:
            await ctx.respond(embed=alert(str(error)), ephemeral=True)
            return
        except Exception as error:
            await self.failed(ctx.interaction, error)
            return
        await ctx.respond(
            embed=fleet_info_embed(), ephemeral=False, allowed_mentions=discord.AllowedMentions.none(),
        )

    async def failed(self, interaction: discord.Interaction, error: Exception) -> None:
        log_event(
            LOGGER,
            "error",
            "fleet_command_failed",
            guild_id=interaction.guild_id,
            user_id=getattr(interaction.user, "id", None),
            **exception_fields(error),
            exc_info=error,
        )
        embed = alert("No pude consultar la flota en este momento. Inténtalo nuevamente más tarde.")
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)


class FleetBoundView(discord.ui.View):
    def __init__(self, cog: FleetCog, guild_id: int, requester_id: int, snapshot: FleetSnapshot) -> None:
        super().__init__(timeout=300, disable_on_timeout=True)
        self.cog = cog
        self.guild_id = guild_id
        self.requester_id = requester_id
        self.snapshot = snapshot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if (
            interaction.guild_id != self.guild_id
            or not isinstance(interaction.user, discord.Member)
            or interaction.user.id != self.requester_id
        ):
            await interaction.response.send_message(
                embed=alert("Abre tu propia consulta con /flota para cambiar las opciones."),
                ephemeral=True,
            )
            return False
        try:
            options = await self.cog.authorize(self.guild_id, interaction.user)
            if options.faction_id != self.snapshot.faction_id:
                raise PermissionError("La configuración cambió. Abre una nueva consulta con /flota.")
            return True
        except PermissionError as error:
            await interaction.response.send_message(embed=alert(str(error)), ephemeral=True)
        except Exception as error:
            await self.cog.failed(interaction, error)
        return False


class FleetModelView(FleetBoundView):
    def __init__(self, cog: FleetCog, guild_id: int, requester_id: int, snapshot: FleetSnapshot) -> None:
        super().__init__(cog, guild_id, requester_id, snapshot)
        self.page = 0
        self.model: str | None = None
        self.refresh()

    def embed(self) -> discord.Embed:
        return discord.Embed(
            title="🚓 Consultar flota",
            description=(
                "Elige un modelo o deja todos. Luego pulsa **Configurar consulta**.\n\n"
                f"**Modelo:** {safe_text(self.model) if self.model else 'Todos'}"
            ),
            color=discord.Color.blurple(),
        ).set_footer(text=f"Modelos · Página {self.page + 1}/{self.page_count}")

    @property
    def page_count(self) -> int:
        return max(1, (len(self.snapshot.models) + MODELS_PER_PAGE - 1) // MODELS_PER_PAGE)

    def refresh(self) -> None:
        self.clear_items()
        start = self.page * MODELS_PER_PAGE
        options = [discord.SelectOption(label="Todos los modelos", value="all", default=self.model is None)]
        options.extend(
            discord.SelectOption(label=name[:100], value=str(index), default=name == self.model)
            for index, name in enumerate(self.snapshot.models[start : start + MODELS_PER_PAGE], start=start)
        )
        picker = discord.ui.Select(placeholder="Modelo del vehículo", options=options, row=0)

        async def select(interaction: discord.Interaction) -> None:
            value = (picker.values or ["all"])[0]
            self.model = None if value == "all" else self.snapshot.models[int(value)]
            self.refresh()
            await interaction.response.edit_message(embed=self.embed(), view=self)

        picker.callback = select  # type: ignore[method-assign]
        self.add_item(picker)
        for direction, label in ((-1, "Anterior"), (1, "Siguiente")):
            button: discord.ui.Button[discord.ui.View] = discord.ui.Button(
                label=label,
                row=1,
                disabled=(self.page == 0 if direction == -1 else self.page + 1 >= self.page_count),
            )

            async def move(interaction: discord.Interaction, step: int = direction) -> None:
                self.page = max(0, min(self.page_count - 1, self.page + step))
                self.refresh()
                await interaction.response.edit_message(embed=self.embed(), view=self)

            button.callback = move  # type: ignore[method-assign]
            self.add_item(button)
        configure: discord.ui.Button[discord.ui.View] = discord.ui.Button(
            label="Configurar consulta",
            style=discord.ButtonStyle.primary,
            row=1,
        )

        async def open_modal(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(FleetModal(self, self.model))

        configure.callback = open_modal  # type: ignore[method-assign]
        self.add_item(configure)


class FleetModal(discord.ui.DesignerModal):
    def __init__(self, owner: FleetModelView, model: str | None) -> None:
        self.owner = owner
        self.model = model
        self.mode = discord.ui.StringSelect(  # type: ignore[type-var]
            options=[
                discord.SelectOption(label=label, value=view.value, default=view is FleetView.SUMMARY)
                for view, label in VIEW_LABELS.items()
            ],
            custom_id="fleet:view",
        )
        self.alpr = discord.ui.StringSelect(  # type: ignore[type-var]
            options=[
                discord.SelectOption(label=label, value=value.value, default=value is AlprFilter.ALL)
                for value, label in (
                    (AlprFilter.ALL, "Todos"),
                    (AlprFilter.EQUIPPED, "Con ALPR"),
                    (AlprFilter.NOT_EQUIPPED, "Sin ALPR"),
                )
            ],
            custom_id="fleet:alpr",
        )
        self.visibility = discord.ui.StringSelect(  # type: ignore[type-var]
            options=[
                discord.SelectOption(label="Solo para mí", value="private", default=True),
                discord.SelectOption(label="Publicar en este canal", value="public"),
            ],
            custom_id="fleet:visibility",
        )
        super().__init__(
            discord.ui.Label("Vista", self.mode),
            discord.ui.Label("Equipamiento ALPR", self.alpr),
            discord.ui.Label("Visibilidad", self.visibility),
            title="Consultar flota",
            timeout=300,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.owner.interaction_check(interaction):
            return
        ephemeral = (self.visibility.values or ["private"])[0] != "public"
        await interaction.response.defer(ephemeral=ephemeral)
        try:
            mode = FleetView((self.mode.values or [FleetView.SUMMARY.value])[0])
            alpr = AlprFilter((self.alpr.values or [AlprFilter.ALL.value])[0])
            drivers = (
                await self.owner.cog.drivers(self.owner.guild_id, self.owner.snapshot)
                if mode is not FleetView.SUMMARY
                else {}
            )
            result = FleetResultView(self.owner, mode, self.model, alpr, drivers)
            await interaction.followup.send(
                embed=result.embed(),
                view=result,
                ephemeral=ephemeral,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as error:
            await self.owner.cog.failed(interaction, error)


class FleetResultView(FleetBoundView):
    def __init__(
        self,
        owner: FleetBoundView,
        mode: FleetView,
        model: str | None,
        alpr: AlprFilter,
        drivers: dict[str, int],
    ) -> None:
        super().__init__(owner.cog, owner.guild_id, owner.requester_id, owner.snapshot)
        self.mode, self.model, self.alpr, self.drivers = mode, model, alpr, drivers
        self.rows = self.snapshot.select(mode, model=model, alpr=alpr)
        self.page = 0
        self.page_count = fleet_page_count(self.rows, mode)
        for direction, label in ((-1, "Anterior"), (1, "Siguiente")):
            button: discord.ui.Button[discord.ui.View] = discord.ui.Button(label=label)

            async def move(interaction: discord.Interaction, step: int = direction) -> None:
                self.page = max(0, min(self.page_count - 1, self.page + step))
                self.refresh()
                await interaction.response.edit_message(
                    embed=self.embed(), view=self, allowed_mentions=discord.AllowedMentions.none()
                )

            button.callback = move  # type: ignore[method-assign]
            self.add_item(button)
        self.refresh()

    def refresh(self) -> None:
        for index, child in enumerate(self.children):
            if isinstance(child, discord.ui.Button):
                child.disabled = self.page == 0 if index == 0 else self.page + 1 >= self.page_count

    def embed(self) -> discord.Embed:
        return fleet_embed(
            self.snapshot,
            self.rows,
            self.mode,
            model=self.model,
            alpr=self.alpr,
            page=self.page,
            drivers=self.drivers,
        )
