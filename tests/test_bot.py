from __future__ import annotations

import pytest

from arrest_report_agent.infrastructure.discord.bot import create_bot


@pytest.mark.asyncio
async def test_restricted_guilds_scope_slash_commands_to_configured_guilds(
    settings, persona
) -> None:
    bot = create_bot(settings, persona)

    assert bot.debug_guilds == sorted(settings.discord_guild_ids or ())


@pytest.mark.asyncio
async def test_open_guild_scope_registers_commands_globally(settings, persona) -> None:
    open_guilds = settings.model_copy(update={"discord_guild_ids_csv": "*"})
    bot = create_bot(open_guilds, persona)

    assert bot.debug_guilds is None
