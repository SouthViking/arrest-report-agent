from __future__ import annotations

from types import SimpleNamespace

import discord

from arrest_report_agent.dedeo.discord import (
    ROY_DEDEO_COPY,
    DedeoCooldown,
    build_dedeo_embed,
    eligible_channel_members,
    select_dedeo_target,
)


class FakeClock:
    def __init__(self) -> None:
        self.current = 100.0

    def __call__(self) -> float:
        return self.current


def _member(user_id: int, *, bot: bool = False, pending: bool = False) -> discord.Member:
    guild = SimpleNamespace(id=1, get_role=lambda _role_id: None, default_role=None)
    state = SimpleNamespace(store_user=lambda data: SimpleNamespace(id=int(data["id"])))
    member = discord.Member(
        data={
            "user": {"id": str(user_id), "username": f"member-{user_id}"},
            "roles": [],
            "pending": pending,
            "bot": bot,
        },
        guild=guild,
        state=state,
    )
    member._user.bot = bot  # noqa: SLF001 - construct a focused Pycord test member
    return member


def test_roy_pool_is_varied_and_every_message_accepts_the_target() -> None:
    assert len(ROY_DEDEO_COPY.titles) >= 20
    assert len(ROY_DEDEO_COPY.messages) >= 20
    assert all(message.count("{target}") == 1 for message in ROY_DEDEO_COPY.messages)
    assert "{seconds}" in ROY_DEDEO_COPY.cooldown


def test_cooldown_is_per_user_and_guild_and_rounds_up_remaining_time() -> None:
    clock = FakeClock()
    cooldown = DedeoCooldown(seconds=30, clock=clock)

    assert cooldown.acquire(guild_id=1, user_id=10) is None
    assert cooldown.acquire(guild_id=1, user_id=10) == 30
    assert cooldown.acquire(guild_id=1, user_id=11) is None
    assert cooldown.acquire(guild_id=2, user_id=10) is None

    clock.current += 10.1
    assert cooldown.acquire(guild_id=1, user_id=10) == 20

    clock.current += 19.9
    assert cooldown.acquire(guild_id=1, user_id=10) is None


def test_eligible_members_are_human_screened_and_can_view_the_channel() -> None:
    visible = _member(1)
    hidden = _member(2)
    bot = _member(3, bot=True)
    pending = _member(4, pending=True)
    channel = SimpleNamespace(
        permissions_for=lambda member: SimpleNamespace(view_channel=member.id != hidden.id)
    )

    result = eligible_channel_members(  # type: ignore[arg-type]
        (visible, hidden, bot, pending),
        channel,
    )

    assert result == (visible,)


def test_target_selection_avoids_the_author_when_another_member_exists(monkeypatch) -> None:
    author = _member(1)
    other = _member(2)
    monkeypatch.setattr("arrest_report_agent.dedeo.discord.secrets.choice", lambda values: values[0])

    assert select_dedeo_target((author, other), author_id=author.id) is other
    assert select_dedeo_target((author,), author_id=author.id) is author
    assert select_dedeo_target((), author_id=author.id) is None


def test_embed_uses_large_avatar_and_local_persona_copy(monkeypatch) -> None:
    monkeypatch.setattr("arrest_report_agent.dedeo.discord.secrets.choice", lambda values: values[0])

    embed = build_dedeo_embed(
        ROY_DEDEO_COPY,
        target_name="Persona *Sospechosa*",
        target_avatar_url="https://cdn.discordapp.com/avatars/1/avatar.png",
    )

    assert embed.title == "☝️ Dedeado"
    assert "Persona \\*Sospechosa\\*" in embed.description
    assert embed.image.url == "https://cdn.discordapp.com/avatars/1/avatar.png"
    assert embed.footer is None
    assert embed.timestamp is None
