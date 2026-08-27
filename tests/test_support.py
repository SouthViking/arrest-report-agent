from __future__ import annotations

from types import SimpleNamespace

import discord

from arrest_report_agent.support import (
    SUPPORT_INLINE_NOTE,
    SUPPORT_URL,
    SupportCog,
    append_support_note,
    build_support_embed,
)


def test_support_command_is_public_and_root_level() -> None:
    assert SupportCog.support.name == "support"
    assert SupportCog.support.parent is None


def test_support_embed_is_concise_linked_and_explicitly_voluntary() -> None:
    embed = build_support_embed()

    assert embed.title == "☕ Apoya el desarrollo"
    assert f"[☕ Cómprame un café]({SUPPORT_URL})" in embed.description
    assert "puedes invitarme un café" in embed.description
    assert "voluntariamente" in embed.description
    assert "no entregan beneficios" in embed.description
    assert embed.author is None
    assert embed.footer is None
    assert embed.thumbnail is None
    assert embed.image is None
    assert embed.timestamp is None


def test_support_note_is_compact_and_idempotent() -> None:
    embed = discord.Embed(description="Contenido principal")

    assert append_support_note(embed) is embed
    assert append_support_note(embed) is embed
    assert embed.description == f"Contenido principal\n\n{SUPPORT_INLINE_NOTE}"
    assert embed.description.count(SUPPORT_URL) == 1


async def test_support_command_replies_privately_without_mentions() -> None:
    responses: list[dict[str, object]] = []

    async def respond(**kwargs: object) -> None:
        responses.append(kwargs)

    context = SimpleNamespace(respond=respond)

    await SupportCog.support.callback(SupportCog(), context)  # type: ignore[arg-type]

    assert responses[0]["ephemeral"] is True
    assert responses[0]["embed"].title == "☕ Apoya el desarrollo"
    assert responses[0]["allowed_mentions"].everyone is False
