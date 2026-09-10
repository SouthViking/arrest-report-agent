from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import discord

from arrest_report_agent.support import (
    SUPPORT_INLINE_NOTE,
    SUPPORT_URL,
    SupportCog,
    SupporterThanksView,
    append_support_note,
    build_acknowledgements_embed,
    build_support_embed,
)
from arrest_report_agent.support_contributions import SupporterRanking


def ranking(
    name: str,
    *,
    discord_user_id: int | None,
    total: str,
    mention_enabled: bool = True,
) -> SupporterRanking:
    return SupporterRanking(
        display_name=name,
        discord_user_id=discord_user_id,
        mention_enabled=mention_enabled,
        total_normalized_usd=Decimal(total),
        contribution_count=1,
        last_contributed_at=datetime(2026, 9, 10, tzinfo=UTC),
    )


class FakeSupportContributionRepository:
    def __init__(self, rankings: tuple[SupporterRanking, ...]) -> None:
        self.rankings = rankings

    async def ranked_supporters(self) -> tuple[SupporterRanking, ...]:
        return self.rankings


def test_support_command_is_public_and_root_level() -> None:
    assert SupportCog.support.name == "support"
    assert SupportCog.support.parent is None
    assert SupportCog.acknowledgements.name == "agradecimientos"
    assert SupportCog.acknowledgements.parent is None


def test_support_embed_is_concise_linked_and_explicitly_voluntary() -> None:
    embed = build_support_embed()

    assert embed.title == "☕ Apoya el desarrollo"
    assert f"[💵 Hacer una donación]({SUPPORT_URL})" in embed.description
    assert "puedes realizar una donación" in embed.description
    assert "voluntariamente" in embed.description
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


async def test_support_command_replies_publicly_without_mentions() -> None:
    responses: list[dict[str, object]] = []

    async def respond(**kwargs: object) -> None:
        responses.append(kwargs)

    context = SimpleNamespace(respond=respond)

    cog = SupportCog(FakeSupportContributionRepository(()))  # type: ignore[arg-type]
    await SupportCog.support.callback(cog, context)  # type: ignore[arg-type]

    assert responses[0]["ephemeral"] is False
    assert responses[0]["embed"].title == "☕ Apoya el desarrollo"
    assert responses[0]["allowed_mentions"].everyone is False


def test_acknowledgements_embed_renders_medals_mentions_and_no_amounts() -> None:
    rankings = (
        ranking("LoMalo", discord_user_id=1520832742047219915, total="19.57"),
        ranking("ydSebas", discord_user_id=393217316722180106, total="2.941"),
        ranking("Sin Discord", discord_user_id=None, total="1.25"),
    )

    embed = build_acknowledgements_embed(rankings)

    assert embed.title == "💛 Agradecimientos"
    assert len(embed.fields) == 1
    assert embed.fields[0].name == "🏆 Principales colaboradores"
    assert embed.fields[0].value == ("🥇 <@1520832742047219915>\n🥈 <@393217316722180106>\n🥉 Sin Discord")
    assert "19.57" not in embed.fields[0].value
    assert "2.941" not in embed.fields[0].value


async def test_acknowledgements_view_keeps_link_and_paginates_rankings() -> None:
    rankings = tuple(
        ranking(f"Persona {index}", discord_user_id=None, total=str(20 - index)) for index in range(12)
    )

    view = SupporterThanksView(requester_id=7, rankings=rankings, page_size=10)

    assert view.page_count == 2
    assert any(isinstance(item, discord.ui.Button) and item.url == SUPPORT_URL for item in view.children)
    assert view.render().footer.text == "Página 1/2"
    view.page = 1
    assert "**11.** Persona 10" in view.render().fields[0].value
    assert "**12.** Persona 11" in view.render().fields[0].value


async def test_acknowledgements_command_is_public_and_disables_mentions() -> None:
    responses: list[dict[str, object]] = []

    async def respond(**kwargs: object) -> None:
        responses.append(kwargs)

    author = SimpleNamespace(id=77)
    context = SimpleNamespace(respond=respond, author=author, guild=None, channel=None)
    repository = FakeSupportContributionRepository((ranking("LoMalo", discord_user_id=123, total="5"),))
    cog = SupportCog(repository)  # type: ignore[arg-type]

    await SupportCog.acknowledgements.callback(cog, context)  # type: ignore[arg-type]

    assert responses[0]["ephemeral"] is False
    assert responses[0]["embed"].title == "💛 Agradecimientos"
    assert responses[0]["allowed_mentions"].to_dict() == (discord.AllowedMentions.none().to_dict())
    view = responses[0]["view"]
    assert isinstance(view, SupporterThanksView)
    assert any(isinstance(item, discord.ui.Button) and item.url == SUPPORT_URL for item in view.children)
