from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import discord
import pytest

from arrest_report_agent.infrastructure.discord.member_directory import (
    DiscordGuildMemberDirectory,
    compact_role_names,
)


def _role(
    role_id: int,
    name: str,
    position: int,
    *,
    managed: bool = False,
    default: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=role_id,
        name=name,
        position=position,
        managed=managed,
        is_default=lambda: default,
    )


def _member(user_id: int, display_name: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        display_name=display_name,
        name=display_name.casefold().replace(" ", "_"),
        roles=[
            _role(1, "@everyone", 0, default=True),
            _role(2, "GNT", 2),
            _role(3, "Deputy Sheriff", 3),
            _role(4, "Integration", 4, managed=True),
        ],
        joined_at=datetime(2026, 1, 2, tzinfo=UTC),
        created_at=datetime(2025, 1, 2, tzinfo=UTC),
        bot=False,
    )


def test_compact_role_names_orders_roles_and_omits_managed_defaults() -> None:
    member = _member(10, "Deputy Torres")

    assert compact_role_names(member) == ["Deputy Sheriff", "GNT"]


@pytest.mark.asyncio
async def test_member_directory_uses_cache_before_fetching() -> None:
    cached_id = 11111111111111111
    fetched_id = 22222222222222222
    cached = _member(cached_id, "Cached Member")
    fetched = _member(fetched_id, "Fetched Member")

    class FakeGuild:
        def __init__(self) -> None:
            self.fetch_calls: list[int] = []

        def get_member(self, user_id: int):
            return cached if user_id == cached_id else None

        async def fetch_member(self, user_id: int):
            self.fetch_calls.append(user_id)
            return fetched

    guild = FakeGuild()
    bot = SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == 99 else None)
    directory = DiscordGuildMemberDirectory(bot)

    result = await directory.lookup_members(99, (cached_id, fetched_id))

    assert guild.fetch_calls == [fetched_id]
    assert set(result.members) == {cached_id, fetched_id}
    assert result.members[cached_id].roles == ("Deputy Sheriff", "GNT")
    assert result.members[fetched_id].display_name == "Fetched Member"
    assert result.not_found == frozenset()
    assert result.unavailable == frozenset()


@pytest.mark.asyncio
async def test_member_directory_does_not_fall_back_to_another_guild() -> None:
    requested_ids = (11111111111111111, 22222222222222222)
    bot = SimpleNamespace(get_guild=lambda guild_id: None)
    directory = DiscordGuildMemberDirectory(bot)

    result = await directory.lookup_members(99, requested_ids)

    assert result.members == {}
    assert result.not_found == frozenset()
    assert result.unavailable == frozenset(requested_ids)


@pytest.mark.asyncio
async def test_member_directory_preserves_partial_not_found_and_unavailable_results() -> None:
    found_id = 11111111111111111
    missing_id = 22222222222222222
    unavailable_id = 33333333333333333

    class FakeGuild:
        def get_member(self, user_id: int):
            return None

        async def fetch_member(self, user_id: int):
            if user_id == found_id:
                return _member(found_id, "Found Member")
            if user_id == missing_id:
                response = SimpleNamespace(status=404, reason="Not Found")
                raise discord.NotFound(response, "Unknown Member")
            response = SimpleNamespace(status=503, reason="Unavailable")
            raise discord.HTTPException(response, "temporary failure")

    guild = FakeGuild()
    bot = SimpleNamespace(get_guild=lambda guild_id: guild)
    directory = DiscordGuildMemberDirectory(bot)

    result = await directory.lookup_members(
        99,
        (found_id, missing_id, unavailable_id),
    )

    assert set(result.members) == {found_id}
    assert result.not_found == frozenset({missing_id})
    assert result.unavailable == frozenset({unavailable_id})
