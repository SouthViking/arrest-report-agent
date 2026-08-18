from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import discord
import pytest

from arrest_report_agent.application.ports import (
    ChannelMemberSearchQuery,
    GuildMemberSearchQuery,
    GuildRoleSearchQuery,
)
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


def _member(
    user_id: int,
    display_name: str,
    *,
    roles: list[SimpleNamespace] | None = None,
    joined_at: datetime | None = None,
    bot: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        display_name=display_name,
        name=display_name.casefold().replace(" ", "_"),
        roles=roles
        or [
            _role(1, "@everyone", 0, default=True),
            _role(2, "GNT", 2),
            _role(3, "Deputy Sheriff", 3),
            _role(4, "Integration", 4, managed=True),
        ],
        joined_at=joined_at or datetime(2026, 1, 2, tzinfo=UTC),
        created_at=datetime(2025, 1, 2, tzinfo=UTC),
        bot=bot,
    )


def _member_query(**updates: object) -> GuildMemberSearchQuery:
    values: dict[str, object] = {
        "query": None,
        "user_ids": (),
        "role_ids_any": (),
        "role_ids_all": (),
        "role_ids_none": (),
        "is_bot": None,
        "joined_after": None,
        "joined_before": None,
        "sort_by": "display_name",
        "sort_direction": "asc",
        "limit": 20,
        "cursor": None,
    }
    values.update(updates)
    return GuildMemberSearchQuery(**values)  # type: ignore[arg-type]


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


@pytest.mark.asyncio
async def test_member_directory_filters_sorts_and_paginates_complete_guild() -> None:
    fto_role = _role(22222222222222222, "Field Training Officer", 5)
    gnt_role = _role(33333333333333333, "GNT", 4)
    everyone = _role(11111111111111111, "@everyone", 0, default=True)
    adriel = _member(
        44444444444444444,
        "Adriel Beltrán",
        roles=[everyone, fto_role, gnt_role],
    )
    alvaro = _member(
        55555555555555555,
        "Álvaro Beltrán",
        roles=[everyone, fto_role],
    )
    outsider = _member(
        66666666666666666,
        "Other Member",
        roles=[everyone, gnt_role],
    )
    guild = SimpleNamespace(
        id=99,
        chunked=True,
        members=[outsider, alvaro, adriel],
        roles=[everyone, gnt_role, fto_role],
    )
    bot = SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == 99 else None)
    directory = DiscordGuildMemberDirectory(bot)
    query = _member_query(
        query="beltran",
        role_ids_all=(fto_role.id,),
        role_ids_none=(gnt_role.id,),
        limit=1,
    )

    first = await directory.search_members(99, query)

    assert first.error is None
    assert [member.display_name for member in first.members] == ["Álvaro Beltrán"]
    assert first.next_cursor is None

    paged_query = _member_query(
        query="beltran",
        role_ids_any=(fto_role.id,),
        limit=1,
    )
    first_page = await directory.search_members(99, paged_query)
    assert [member.display_name for member in first_page.members] == ["Adriel Beltrán"]
    assert first_page.next_cursor is not None

    second_page = await directory.search_members(
        99,
        _member_query(
            query="beltran",
            role_ids_any=(fto_role.id,),
            limit=1,
            cursor=first_page.next_cursor,
        ),
    )
    assert [member.display_name for member in second_page.members] == ["Álvaro Beltrán"]
    assert second_page.next_cursor is None

    invalid = await directory.search_members(
        99,
        _member_query(query="different", cursor=first_page.next_cursor),
    )
    assert invalid.error == "invalid_cursor"

    conflicting = await directory.search_members(
        99,
        _member_query(
            role_ids_any=(fto_role.id,),
            role_ids_none=(fto_role.id,),
        ),
    )
    assert conflicting.error == "invalid_member_filters"


@pytest.mark.asyncio
async def test_member_directory_prefers_full_name_over_abbreviated_alias() -> None:
    exact = _member(11111111111111111, "Derek Valdez")
    abbreviated = _member(22222222222222222, "D. Valdez / Michael Spencer")
    guild = SimpleNamespace(id=99, chunked=True, members=[abbreviated, exact], roles=[])
    directory = DiscordGuildMemberDirectory(SimpleNamespace(get_guild=lambda guild_id: guild))

    result = await directory.search_members(99, _member_query(query="Derek Valdez"))

    assert [member.user_id for member in result.members] == [exact.id]
    assert result.name_match_strategy == "exact_terms"
    assert result.candidate_count == 1


@pytest.mark.asyncio
async def test_member_directory_matches_initial_and_surname_in_character_aliases() -> None:
    derek = _member(11111111111111111, "D. Valdez / Michael Spencer")
    unrelated = _member(22222222222222222, "D. Velasco / Derek Morgan")
    guild = SimpleNamespace(id=99, chunked=True, members=[unrelated, derek], roles=[])
    directory = DiscordGuildMemberDirectory(SimpleNamespace(get_guild=lambda guild_id: guild))

    result = await directory.search_members(99, _member_query(query="Derek Valdez"))

    assert [member.user_id for member in result.members] == [derek.id]
    assert result.name_match_strategy == "initial_and_surname"
    assert result.candidate_count == 1


@pytest.mark.asyncio
async def test_member_directory_preserves_ambiguous_abbreviated_candidates() -> None:
    first = _member(11111111111111111, "D. Valdez / Michael Spencer")
    second = _member(22222222222222222, "D. Valdez / Chloe Miller")
    guild = SimpleNamespace(id=99, chunked=True, members=[second, first], roles=[])
    directory = DiscordGuildMemberDirectory(SimpleNamespace(get_guild=lambda guild_id: guild))

    result = await directory.search_members(99, _member_query(query="Derek Valdez"))

    assert {member.user_id for member in result.members} == {first.id, second.id}
    assert result.name_match_strategy == "initial_and_surname"
    assert result.candidate_count == 2


@pytest.mark.asyncio
async def test_member_directory_returns_surname_only_as_weak_fallback() -> None:
    possible = _member(11111111111111111, "M. Valdez / Michael Spencer")
    guild = SimpleNamespace(id=99, chunked=True, members=[possible], roles=[])
    directory = DiscordGuildMemberDirectory(SimpleNamespace(get_guild=lambda guild_id: guild))

    result = await directory.search_members(99, _member_query(query="Derek Valdez"))

    assert [member.user_id for member in result.members] == [possible.id]
    assert result.name_match_strategy == "surname"
    assert result.candidate_count == 1


@pytest.mark.asyncio
async def test_member_directory_lists_roles_with_counts_and_filters() -> None:
    fto_role = _role(22222222222222222, "Field Training Officer", 5)
    gnt_role = _role(33333333333333333, "GNT", 4)
    managed = _role(44444444444444444, "Integration", 6, managed=True)
    everyone = _role(11111111111111111, "@everyone", 0, default=True)
    guild = SimpleNamespace(
        id=99,
        chunked=True,
        roles=[everyone, gnt_role, fto_role, managed],
        members=[
            _member(55555555555555555, "One", roles=[everyone, fto_role]),
            _member(
                66666666666666666,
                "Two",
                roles=[everyone, fto_role, gnt_role, managed],
            ),
        ],
    )
    directory = DiscordGuildMemberDirectory(
        SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == 99 else None)
    )

    result = await directory.search_roles(
        99,
        GuildRoleSearchQuery(
            query="training",
            role_ids=(),
            include_managed=False,
            sort_by="member_count",
            sort_direction="desc",
            limit=25,
            cursor=None,
        ),
    )

    assert result.error is None
    assert [(role.name, role.member_count) for role in result.roles] == [("Field Training Officer", 2)]

    managed_result = await directory.search_roles(
        99,
        GuildRoleSearchQuery(
            query=None,
            role_ids=(managed.id,),
            include_managed=True,
            sort_by="position",
            sort_direction="desc",
            limit=25,
            cursor=None,
        ),
    )
    assert [(role.name, role.member_count) for role in managed_result.roles] == [("Integration", 1)]


class FakeTextChannel:
    def __init__(self, channel_id: int, guild: object) -> None:
        self.id = channel_id
        self.guild = guild
        self.messages: list[SimpleNamespace] = []

    def permissions_for(self, member: object) -> SimpleNamespace:
        return SimpleNamespace(view_channel=True, read_message_history=True)

    async def history(self, **kwargs):
        for message in self.messages[: int(kwargs["limit"])]:
            yield message


@pytest.mark.asyncio
async def test_channel_member_search_uses_active_guild_and_recent_authors(monkeypatch) -> None:
    guild_placeholder = SimpleNamespace(id=99)
    channel = FakeTextChannel(77777777777777777, guild_placeholder)
    requester = _member(88888888888888888, "Requester")
    participant = _member(99999999999999999, "Participant")
    guild = SimpleNamespace(
        id=99,
        chunked=True,
        members=[requester, participant],
        get_member=lambda user_id: {
            requester.id: requester,
            participant.id: participant,
        }.get(user_id),
        get_channel_or_thread=lambda channel_id: channel if channel_id == channel.id else None,
    )
    channel.guild = guild
    channel.messages = [SimpleNamespace(author=participant)]
    bot = SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == 99 else None)
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.member_directory.discord.TextChannel",
        FakeTextChannel,
    )
    directory = DiscordGuildMemberDirectory(bot)

    result = await directory.search_channel_members(
        guild_id=99,
        requester_user_id=requester.id,
        current_channel_id=channel.id,
        query=ChannelMemberSearchQuery(
            channel_id=channel.id,
            membership="recent_participants",
            message_limit=50,
            members=_member_query(),
        ),
    )

    assert result.error is None
    assert [member.user_id for member in result.members] == [participant.id]


@pytest.mark.asyncio
async def test_channel_member_search_rejects_channel_from_another_guild() -> None:
    requester = _member(88888888888888888, "Requester")
    foreign_channel = SimpleNamespace(
        id=77777777777777777,
        guild=SimpleNamespace(id=100),
    )
    guild = SimpleNamespace(
        id=99,
        get_member=lambda user_id: requester,
        get_channel_or_thread=lambda channel_id: foreign_channel,
    )
    directory = DiscordGuildMemberDirectory(
        SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == 99 else None)
    )

    result = await directory.search_channel_members(
        guild_id=99,
        requester_user_id=requester.id,
        current_channel_id=foreign_channel.id,
        query=ChannelMemberSearchQuery(
            channel_id=foreign_channel.id,
            membership="can_view",
            message_limit=1,
            members=_member_query(),
        ),
    )

    assert result.error == "channel_outside_guild"
