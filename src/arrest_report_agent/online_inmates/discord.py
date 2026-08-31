"""Public Discord command for connected characters currently in prison."""

from __future__ import annotations

import logging
import math

import discord
from discord.ext import commands

from arrest_report_agent.config import BotPersona
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider

from .models import OnlineInmateSnapshot
from .service import OnlineInmateService

LOGGER = logging.getLogger(__name__)
VIEW_TIMEOUT_SECONDS = 300.0


class OnlineInmateCog(commands.Cog):
    """Expose the connected inmate snapshot to operational staff."""

    def __init__(
        self,
        provider: DatabaseConfigProvider,
        service: OnlineInmateService,
        persona: BotPersona,
        *,
        page_size: int = 10,
    ) -> None:
        if not 1 <= page_size <= 25:
            raise ValueError("Online inmate page size must be between 1 and 25")
        self.provider = provider
        self.service = service
        self.default_persona = persona
        self.page_size = page_size

    @discord.slash_command(
        name="internos",
        description="Muestra los personajes conectados que están en prisión",
    )
    async def online_inmates(self, ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            await ctx.respond(
                embed=_alert(
                    "⚠️ Consulta no disponible",
                    "Este comando solo funciona dentro de un servidor.",
                ),
                ephemeral=True,
            )
            return

        guild_id = ctx.guild.id
        user_id = ctx.author.id
        deferred = False
        ui = self.default_persona.messages.interface.faction_status
        try:
            bundle = await self.provider.cached(guild_id)
            ui = bundle.persona.profile.messages.interface.faction_status
            if not _member_has_any_role(
                ctx.author,
                bundle.settings.operational_staff_role_ids,
            ):
                await ctx.respond(
                    embed=_alert("⚠️ Consulta no disponible", ui.unauthorized),
                    ephemeral=True,
                )
                return

            await ctx.defer(ephemeral=False)
            deferred = True
            snapshot = await self.service.query()
            view = OnlineInmateView(user_id, snapshot, page_size=self.page_size)
            if view.page_count > 1:
                await ctx.followup.send(
                    embed=view.render(),
                    view=view,
                    ephemeral=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await ctx.followup.send(
                    embed=view.render(),
                    ephemeral=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            log_event(
                LOGGER,
                "info",
                "online_inmates_published",
                guild_id=guild_id,
                user_id=user_id,
                server_connected_count=snapshot.server_connected_count,
                inmate_count=snapshot.inmate_count,
            )
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "online_inmates_command_failed",
                guild_id=guild_id,
                user_id=user_id,
                exc_info=error,
                **exception_fields(error),
            )
            embed = _alert("⚠️ Consulta no disponible", ui.operation_failed)
            if deferred:
                await ctx.followup.send(embed=embed, ephemeral=True)
            else:
                await ctx.respond(embed=embed, ephemeral=True)


class OnlineInmateView(discord.ui.View):
    """Navigate one immutable snapshot without repeating the SARP request."""

    def __init__(
        self,
        requester_id: int,
        snapshot: OnlineInmateSnapshot,
        *,
        page_size: int,
    ) -> None:
        super().__init__(timeout=VIEW_TIMEOUT_SECONDS)
        self.requester_id = requester_id
        self.snapshot = snapshot
        self.page_size = page_size
        self.page = 0
        self._sync_buttons()

    @property
    def page_count(self) -> int:
        return max(1, math.ceil(self.snapshot.inmate_count / self.page_size))

    def render(self) -> discord.Embed:
        return build_online_inmates_embed(
            self.snapshot,
            page=self.page,
            page_size=self.page_size,
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user is not None and interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            embed=_alert(
                "⚠️ Navegación restringida",
                "Solo quien abrió esta consulta puede cambiar de página.",
            ),
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Anterior", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous(
        self,
        _button: discord.ui.Button[OnlineInmateView],
        interaction: discord.Interaction,
    ) -> None:
        self.page = max(0, self.page - 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.render(), view=self)

    @discord.ui.button(label="Siguiente", emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next(
        self,
        _button: discord.ui.Button[OnlineInmateView],
        interaction: discord.Interaction,
    ) -> None:
        self.page = min(self.page_count - 1, self.page + 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.render(), view=self)

    def _sync_buttons(self) -> None:
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= self.page_count - 1


def build_online_inmates_embed(
    snapshot: OnlineInmateSnapshot,
    *,
    page: int = 0,
    page_size: int = 10,
) -> discord.Embed:
    """Render one compact page without exposing account or character metadata."""

    page_count = max(1, math.ceil(snapshot.inmate_count / page_size))
    safe_page = min(max(page, 0), page_count - 1)
    summary = (
        f"**Servidor:** {snapshot.server_connected_count} conectados · "
        f"**En prisión:** {snapshot.inmate_count}"
    )
    if snapshot.inmate_names:
        start = safe_page * page_size
        displayed = snapshot.inmate_names[start : start + page_size]
        names = "\n".join(discord.utils.escape_markdown(name) for name in displayed)
        description = f"{summary}\n\n**Listado**\n{names}"
    else:
        description = f"{summary}\n\nActualmente no hay personajes conectados en prisión."

    embed = discord.Embed(
        title="⛓️ Internos conectados",
        description=description,
        color=discord.Color.dark_grey(),
    )
    if page_count > 1:
        embed.set_footer(text=f"Página {safe_page + 1}/{page_count}")
    return embed


def _member_has_any_role(member: discord.Member, role_ids: tuple[int, ...]) -> bool:
    allowed = set(role_ids)
    return bool(allowed and any(role.id in allowed for role in member.roles))


def _alert(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=discord.Color.red())
