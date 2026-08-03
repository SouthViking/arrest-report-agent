from __future__ import annotations

from arrest_report_agent.infrastructure.discord.cog import (
    EMBED_SPACER,
    ReviewCog,
    build_status_embed,
)


def test_operational_command_group_uses_distinct_wv_namespace() -> None:
    assert ReviewCog.wv.name == "wv"


def test_status_embed_uses_star_and_spaced_two_column_rows() -> None:
    embed = build_status_embed(
        display_name="Walter Vigil",
        discord_ready=True,
        environment="local",
        model="gpt-5.6-terra",
        charge_count=164,
        guide_version="2026.08",
        active_reviews=2,
    )

    assert embed.title == "⭐ Estado · Walter Vigil"
    assert [field.name for field in embed.fields] == [
        "Discord",
        EMBED_SPACER,
        "Entorno",
        "Modelo",
        EMBED_SPACER,
        "Catálogo",
        "Guía",
        EMBED_SPACER,
        "Revisiones activas",
    ]
    assert all(field.inline for field in embed.fields)
