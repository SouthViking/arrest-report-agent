from __future__ import annotations

from arrest_report_agent.infrastructure.discord.cog import (
    EMBED_SPACER,
    PENAL_CODE_URL,
    PROCEDURES_MANUAL_URL,
    ReviewCog,
    build_help_embed,
    build_status_embed,
)


def test_operational_command_group_uses_distinct_wv_namespace() -> None:
    assert ReviewCog.wv.name == "wv"
    assert {command.name for command in ReviewCog.wv.subcommands} == {"help", "status"}


def test_help_embed_uses_runtime_identity_channels_and_bot_mention() -> None:
    embed = build_help_embed(
        display_name="Sgt. Rivera",
        role_title="Watch Sergeant",
        bot_mention="<@999>",
        channel_ids=frozenset({222, 111}),
        avatar_url="https://cdn.discordapp.com/avatar.png",
    )

    assert embed.title == "⭐ Sgt. Rivera · Watch Sergeant"
    assert "Sgt. Rivera" in embed.description
    assert [field.name for field in embed.fields] == [
        "📋 Qué reviso",
        "📍 Dónde estoy de guardia",
        "📣 Cómo solicitar una revisión",
        "🧵 Qué haré con el expediente",
        "📚 Documentación oficial",
    ]
    assert embed.fields[1].value == "• <#111>\n• <#222>"
    assert "<@999>" in embed.fields[2].value
    assert embed.fields[4].value == (
        f"• [Código Penal]({PENAL_CODE_URL})\n"
        f"• [Manual de Procedimientos]({PROCEDURES_MANUAL_URL})"
    )
    assert embed.thumbnail.url == "https://cdn.discordapp.com/avatar.png"
    assert "Walter Vigil" not in str(embed.to_dict())


def test_status_embed_uses_star_and_spaced_two_column_rows() -> None:
    embed = build_status_embed(
        display_name="Walter Vigil",
        discord_ready=True,
        environment="local",
        model="gpt-5.6-terra",
        charge_count=164,
        guide_version="2026.08",
        active_reviews=2,
        channel_permission_lines=(
            "✅ <#111> — permisos completos",
            "❌ <#222> — faltan: gestionar hilos",
        ),
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
        "🛡️ Canales y permisos",
    ]
    assert all(field.inline for field in embed.fields[:-1])
    assert embed.fields[-1].inline is False
    assert embed.fields[-1].value == (
        "✅ <#111> — permisos completos\n❌ <#222> — faltan: gestionar hilos"
    )
