"""Public, non-intrusive access to voluntary project support."""

from __future__ import annotations

import logging
import math

import discord
from discord.ext import commands

from arrest_report_agent.config.instance import SupportOptions
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider
from arrest_report_agent.support_contributions import (
    SupportContributionRepository,
    SupporterRanking,
)

SUPPORTER_PAGE_SIZE = 10
VIEW_TIMEOUT_SECONDS = 300.0
MEDALS = ("🥇", "🥈", "🥉")
LOGGER = logging.getLogger(__name__)


class SupportCog(commands.Cog):
    """Expose one public shortcut without posting automatic promotions."""

    def __init__(
        self, repository: SupportContributionRepository, provider: DatabaseConfigProvider,
    ) -> None:
        self.repository = repository
        self.provider = provider

    async def _options(self, ctx: discord.ApplicationContext) -> SupportOptions | None:
        options = None
        if ctx.guild is not None:
            bundle = await self.provider.cached(ctx.guild.id)
            options = bundle.settings.support_options
        if options is None:
            await ctx.respond(
                "Los aportes voluntarios no están habilitados en este servidor.", ephemeral=True,
            )
        return options

    @discord.slash_command(
        name="support",
        description="Apoya voluntariamente el desarrollo y mantenimiento del bot",
    )
    async def support(self, ctx: discord.ApplicationContext) -> None:
        options = await self._options(ctx)
        if options is None:
            return
        await ctx.respond(
            embed=build_support_embed(str(options.url)),
            ephemeral=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.slash_command(
        name="agradecimientos",
        description="Muestra a quienes han apoyado voluntariamente el bot",
    )
    async def acknowledgements(self, ctx: discord.ApplicationContext) -> None:
        options = await self._options(ctx)
        if options is None:
            return
        try:
            rankings = await self.repository.ranked_supporters()
            view = SupporterThanksView(
                requester_id=ctx.author.id,
                rankings=rankings,
                support_url=str(options.url),
            )
            await ctx.respond(
                embed=view.render(),
                view=view,
                ephemeral=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log_event(
                LOGGER,
                "info",
                "supporter_acknowledgements_published",
                guild_id=ctx.guild.id if ctx.guild is not None else None,
                channel_id=ctx.channel.id if ctx.channel is not None else None,
                user_id=ctx.author.id,
                supporter_count=len(rankings),
            )
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "supporter_acknowledgements_failed",
                exc_info=error,
                guild_id=ctx.guild.id if ctx.guild is not None else None,
                channel_id=ctx.channel.id if ctx.channel is not None else None,
                user_id=ctx.author.id,
                **exception_fields(error),
            )
            await ctx.respond(
                embed=discord.Embed(
                    title="⚠️ No pude abrir los agradecimientos",
                    description=("Se trabó la lista de gente generosa. Inténtalo nuevamente en un momento."),
                    color=discord.Color.red(),
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )


class SupporterThanksView(discord.ui.View):
    """Navigate an immutable public ranking without sending mention notifications."""

    def __init__(
        self,
        *,
        requester_id: int,
        rankings: tuple[SupporterRanking, ...],
        page_size: int = SUPPORTER_PAGE_SIZE,
        support_url: str | None = None,
    ) -> None:
        if not 1 <= page_size <= 25:
            raise ValueError("Supporter page size must be between 1 and 25")
        super().__init__(timeout=VIEW_TIMEOUT_SECONDS)
        self.requester_id = requester_id
        self.rankings = rankings
        self.page_size = page_size
        self.page = 0
        if support_url is not None:
            self.add_item(
                discord.ui.Button(
                    label="Cómprame un café", emoji="💵",
                    style=discord.ButtonStyle.link, url=support_url,
                )
            )
        if self.page_count <= 1:
            self.remove_item(self.previous)
            self.remove_item(self.next)
        else:
            self._sync_buttons()

    @property
    def page_count(self) -> int:
        return max(1, math.ceil(len(self.rankings) / self.page_size))

    def render(self) -> discord.Embed:
        return build_acknowledgements_embed(
            self.rankings,
            page=self.page,
            page_size=self.page_size,
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user is not None and interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            embed=discord.Embed(
                title="⚠️ Navegación restringida",
                description="Solo quien abrió esta lista puede cambiar de página.",
                color=discord.Color.red(),
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    @discord.ui.button(label="Anterior", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous(
        self,
        _button: discord.ui.Button[SupporterThanksView],
        interaction: discord.Interaction,
    ) -> None:
        self.page = max(0, self.page - 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.render(), view=self)

    @discord.ui.button(label="Siguiente", emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next(
        self,
        _button: discord.ui.Button[SupporterThanksView],
        interaction: discord.Interaction,
    ) -> None:
        self.page = min(self.page_count - 1, self.page + 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.render(), view=self)

    def _sync_buttons(self) -> None:
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= self.page_count - 1


def build_support_embed(support_url: str) -> discord.Embed:
    """Build a concise card that makes the voluntary nature explicit."""

    return discord.Embed(
        title="☕ Apoya el desarrollo",
        description=(
            "Si el bot te resulta útil y quieres apoyar voluntariamente su desarrollo y "
            "mantenimiento, puedes realizar una donación.\n\n"
            f"[💵 Hacer una donación]({support_url})"
        ),
        color=discord.Color.gold(),
    )


def build_acknowledgements_embed(
    rankings: tuple[SupporterRanking, ...],
    *,
    page: int = 0,
    page_size: int = SUPPORTER_PAGE_SIZE,
) -> discord.Embed:
    """Render supporter identities while deliberately omitting monetary amounts."""

    page_count = max(1, math.ceil(len(rankings) / page_size))
    safe_page = min(max(page, 0), page_count - 1)
    start = safe_page * page_size
    displayed = rankings[start : start + page_size]
    description = (
        "Gracias a quienes han apoyado voluntariamente el desarrollo y mantenimiento "
        "del bot. Sí, sorprendentemente alguien decidió financiar esta criatura."
    )
    embed = discord.Embed(
        title="💛 Agradecimientos",
        description=description,
        color=discord.Color.gold(),
    )
    if displayed:
        lines = [
            _supporter_line(ranking, position=start + index)
            for index, ranking in enumerate(displayed, start=1)
        ]
        embed.add_field(
            name=("🏆 Principales colaboradores" if safe_page == 0 else "💛 Colaboradores"),
            value="\n".join(lines),
            inline=False,
        )
    else:
        embed.add_field(
            name="Todavía no hay nombres en la lista",
            value="Puedes ser la primera persona en aparecer por aquí.",
            inline=False,
        )
    if page_count > 1:
        embed.set_footer(text=f"Página {safe_page + 1}/{page_count}")
    return embed


def _supporter_line(ranking: SupporterRanking, *, position: int) -> str:
    marker = MEDALS[position - 1] if position <= len(MEDALS) else f"**{position}.**"
    if ranking.mention_enabled and ranking.discord_user_id is not None:
        identity = f"<@{ranking.discord_user_id}>"
    else:
        identity = discord.utils.escape_markdown(ranking.display_name or "Colaborador anónimo")
    return f"{marker} {identity}"


def append_support_note(
    embed: discord.Embed, options: SupportOptions | None = None,
) -> discord.Embed:
    """Append the compact link once to a suitable public informational embed."""

    if options is None:
        return embed
    url = str(options.url)
    description = embed.description or ""
    if url in description:
        return embed
    note = f"💵 ¿Te sirve el bot? [Haz una donación]({url})"
    embed.description = f"{description}\n\n{note}".lstrip()
    return embed
