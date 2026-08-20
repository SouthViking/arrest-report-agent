from __future__ import annotations

from types import SimpleNamespace

import discord
import pytest

from arrest_report_agent.config import ForumShortcutsOptions
from arrest_report_agent.forum_shortcuts import (
    ForumShortcutsCog,
    build_forum_shortcuts_embed,
)


def _options() -> ForumShortcutsOptions:
    return ForumShortcutsOptions.model_validate(
        {
            "organization_name": "Los Santos Sheriff's Department",
            "description": "Consulta rápidamente nuestros espacios oficiales.",
            "banner_url": "https://i.imgur.com/gBxZZIT.gif",
            "sections": [
                {
                    "title": "🏛️ Foro institucional",
                    "links": [
                        {
                            "label": "Página principal",
                            "url": "https://sheriff.sarp.es/index.php",
                        }
                    ],
                },
                {
                    "title": "👥 Ingreso y desarrollo",
                    "links": [
                        {
                            "label": "Oportunidades de empleo",
                            "url": "https://sheriff.sarp.es/viewforum.php?f=3",
                        },
                        {
                            "label": "Reincorporaciones",
                            "url": "https://sheriff.sarp.es/viewtopic.php?t=104",
                        },
                    ],
                },
            ],
        }
    )


def test_forum_shortcut_options_reject_duplicate_urls() -> None:
    payload = _options().model_dump(mode="json")
    payload["sections"][1]["links"][0]["url"] = payload["sections"][0]["links"][0]["url"]

    with pytest.raises(ValueError, match="URLs must be unique"):
        ForumShortcutsOptions.model_validate(payload)


def test_forum_embed_is_neutral_grouped_and_uses_the_configured_gif() -> None:
    embed = build_forum_shortcuts_embed(
        _options(),
        thumbnail_url="https://cdn.discordapp.com/icons/1/icon.png",
    )

    assert embed.author.name == "Los Santos Sheriff's Department — Foro"
    assert embed.title == "🔗 Accesos oficiales"
    assert embed.colour == discord.Color.from_rgb(0, 82, 14)
    assert embed.description == "Consulta rápidamente nuestros espacios oficiales."
    assert embed.fields[0].name == "🏛️ Foro institucional"
    assert "└─ [Página principal](https://sheriff.sarp.es/index.php)" in (embed.fields[0].value)
    assert embed.fields[1].name == "👥 Ingreso y desarrollo"
    assert "Oportunidades de empleo" in embed.fields[1].value
    assert embed.image.url == "https://i.imgur.com/gBxZZIT.gif"
    assert embed.thumbnail.url == "https://cdn.discordapp.com/icons/1/icon.png"
    assert embed.footer.text == "Foro oficial · Los Santos Sheriff's Department"


async def test_forum_command_is_public_and_visible_to_everyone() -> None:
    responses: list[dict[str, object]] = []
    options = _options()

    async def cached(guild_id: int):
        assert guild_id == 1
        return SimpleNamespace(
            settings=SimpleNamespace(forum_shortcuts_options=options),
        )

    async def respond(**kwargs: object) -> None:
        responses.append(kwargs)

    cog = ForumShortcutsCog(SimpleNamespace(cached=cached))
    context = SimpleNamespace(
        guild=SimpleNamespace(id=1, icon=None),
        channel=SimpleNamespace(id=2),
        author=SimpleNamespace(id=3, roles=()),
        respond=respond,
    )

    await ForumShortcutsCog.forum.callback(cog, context)  # type: ignore[arg-type]

    assert len(responses) == 1
    assert responses[0]["ephemeral"] is False
    assert responses[0]["embed"].title == "🔗 Accesos oficiales"
    assert responses[0]["allowed_mentions"].everyone is False


async def test_forum_command_reports_disabled_module_without_persona_copy() -> None:
    responses: list[dict[str, object]] = []

    async def cached(_guild_id: int):
        return SimpleNamespace(
            settings=SimpleNamespace(forum_shortcuts_options=None),
        )

    async def respond(**kwargs: object) -> None:
        responses.append(kwargs)

    cog = ForumShortcutsCog(SimpleNamespace(cached=cached))
    context = SimpleNamespace(
        guild=SimpleNamespace(id=1, icon=None),
        channel=SimpleNamespace(id=2),
        author=SimpleNamespace(id=3),
        respond=respond,
    )

    await ForumShortcutsCog.forum.callback(cog, context)  # type: ignore[arg-type]

    assert responses[0]["ephemeral"] is True
    assert responses[0]["embed"].title == "⚠️ Foro no disponible"
    assert "este servidor" in responses[0]["embed"].description
