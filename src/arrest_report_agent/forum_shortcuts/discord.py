"""Discord command for guild-configured official forum links."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from arrest_report_agent.config import ForumShortcutsOptions
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider

LOGGER = logging.getLogger(__name__)


class ForumShortcutsCog(commands.Cog):
    """Expose one public, neutral and guild-scoped forum directory."""

    def __init__(self, provider: DatabaseConfigProvider) -> None:
        self.provider = provider

    @discord.slash_command(
        name="foro",
        description="Muestra los accesos oficiales del foro",
    )
    async def forum(self, ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None:
            await ctx.respond(
                embed=_alert(
                    "⚠️ Foro no disponible",
                    "Este comando solo funciona dentro de un servidor.",
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        guild_id = ctx.guild.id
        user_id = ctx.author.id
        try:
            bundle = await self.provider.cached(guild_id)
            options = bundle.settings.forum_shortcuts_options
            if options is None:
                await ctx.respond(
                    embed=_alert(
                        "⚠️ Foro no disponible",
                        "Los accesos al foro no están habilitados para este servidor.",
                    ),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            thumbnail_url = str(ctx.guild.icon.url) if ctx.guild.icon is not None else None
            await ctx.respond(
                embed=build_forum_shortcuts_embed(
                    options,
                    thumbnail_url=thumbnail_url,
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log_event(
                LOGGER,
                "info",
                "forum_shortcuts_opened",
                guild_id=guild_id,
                channel_id=ctx.channel.id if ctx.channel is not None else "unknown",
                user_id=user_id,
                link_count=sum(len(section.links) for section in options.sections),
            )
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "forum_shortcuts_command_failed",
                guild_id=guild_id,
                user_id=user_id,
                exc_info=error,
                **exception_fields(error),
            )
            await ctx.respond(
                embed=_alert(
                    "⚠️ Foro no disponible",
                    "No fue posible preparar los accesos al foro en este momento.",
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )


def build_forum_shortcuts_embed(
    options: ForumShortcutsOptions,
    *,
    thumbnail_url: str | None = None,
) -> discord.Embed:
    """Render the bounded institutional panel without persona copy."""

    embed = discord.Embed(
        title="🔗 Accesos oficiales",
        description=options.description,
        color=discord.Color.from_rgb(0, 82, 14),
    )
    embed.set_author(name=f"{options.organization_name} — Foro")
    for section in options.sections:
        embed.add_field(
            name=section.title,
            value="\n".join(
                f"└─ [{discord.utils.escape_markdown(link.label)}]({link.url})" for link in section.links
            ),
            inline=False,
        )
    embed.set_image(url=str(options.banner_url))
    embed.set_footer(text=f"Foro oficial · {options.organization_name}")
    if thumbnail_url is not None:
        embed.set_thumbnail(url=thumbnail_url)
    return embed


def _alert(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=discord.Color.red())
