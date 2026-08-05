"""Discord bot construction."""

from __future__ import annotations

import discord

from arrest_report_agent.config import AppSettings, BotPersona


def create_bot(settings: AppSettings, persona: BotPersona) -> discord.Bot:
    """Create a bot with only the intents required by the review workflow."""

    guild_ids = settings.guild_allowlist.ids
    intents = discord.Intents.none()
    intents.guilds = True
    intents.messages = True
    intents.message_content = True
    return discord.Bot(
        intents=intents,
        description=f"{persona.identity.display_name} · Revisión de reportes de arresto",
        debug_guilds=sorted(guild_ids) if guild_ids is not None else None,
    )
