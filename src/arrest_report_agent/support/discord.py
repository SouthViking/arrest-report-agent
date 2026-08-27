"""Public, non-intrusive access to voluntary project support."""

from __future__ import annotations

import discord
from discord.ext import commands

SUPPORT_URL = "https://buymeacoffee.com/southviking"
SUPPORT_INLINE_NOTE = f"💵 ¿Te sirve el bot? [Haz una donación]({SUPPORT_URL})"


class SupportCog(commands.Cog):
    """Expose one public shortcut without posting automatic promotions."""

    @discord.slash_command(
        name="support",
        description="Apoya voluntariamente el desarrollo y mantenimiento del bot",
    )
    async def support(self, ctx: discord.ApplicationContext) -> None:
        await ctx.respond(
            embed=build_support_embed(),
            ephemeral=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )


def build_support_embed() -> discord.Embed:
    """Build a concise card that makes the voluntary nature explicit."""

    return discord.Embed(
        title="☕ Apoya el desarrollo",
        description=(
            "Si el bot te resulta útil y quieres apoyar voluntariamente su desarrollo y "
            "mantenimiento, puedes realizar una donación.\n\n"
            f"[💵 Hacer una donación]({SUPPORT_URL})"
        ),
        color=discord.Color.gold(),
    )


def append_support_note(embed: discord.Embed) -> discord.Embed:
    """Append the compact link once to a suitable public informational embed."""

    description = embed.description or ""
    if SUPPORT_URL in description:
        return embed
    embed.description = f"{description}\n\n{SUPPORT_INLINE_NOTE}".lstrip()
    return embed
