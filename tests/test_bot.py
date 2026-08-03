from __future__ import annotations

from arrest_report_agent.infrastructure.discord.bot import create_bot


def test_restricted_guilds_scope_slash_commands_to_configured_guilds(settings) -> None:
    bot = create_bot(settings)

    assert bot.debug_guilds == sorted(settings.discord_guild_ids or ())


def test_open_guild_scope_registers_commands_globally(settings) -> None:
    open_guilds = settings.model_copy(update={"discord_guild_ids_csv": "*"})
    bot = create_bot(open_guilds)

    assert bot.debug_guilds is None
