from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from arrest_report_agent.infrastructure.discord.profile import DiscordProfileSynchronizer


class FakeClientUser:
    def __init__(self, name: str, field_failures: dict[str, int] | None = None) -> None:
        self.name = name
        self.field_failures = dict(field_failures or {})
        self.edits: list[dict[str, object]] = []

    async def edit(self, **fields):
        self.edits.append(fields)
        field = next(iter(fields))
        if self.field_failures.get(field, 0):
            self.field_failures[field] -= 1
            raise RuntimeError("Discord profile unavailable")
        return self


class FakeMember:
    def __init__(self, nick: str | None = None, failures: int = 0) -> None:
        self.nick = nick
        self.failures = failures
        self.edits: list[dict[str, object]] = []

    async def edit(self, **fields):
        self.edits.append(fields)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("Discord guild profile unavailable")
        return self


@pytest.mark.asyncio
async def test_profile_sync_applies_global_fields_once_per_process(
    loaded_persona,
    global_banner_bytes,
) -> None:
    user = FakeClientUser("Old Name")
    bot = SimpleNamespace(user=user, guilds=[])
    synchronizer = DiscordProfileSynchronizer(
        bot,
        loaded_persona,
        global_banner_bytes=global_banner_bytes,
    )

    await synchronizer.synchronize()
    await synchronizer.synchronize()

    assert synchronizer.global_profile_synchronized is True
    assert user.edits == [
        {"username": "Walter Vigil"},
        {"avatar": loaded_persona.avatar_bytes},
        {"banner": global_banner_bytes},
    ]


@pytest.mark.asyncio
async def test_profile_sync_omits_unchanged_username(
    loaded_persona,
    global_banner_bytes,
) -> None:
    user = FakeClientUser(loaded_persona.profile.discord.username)
    synchronizer = DiscordProfileSynchronizer(
        SimpleNamespace(user=user, guilds=[]),
        loaded_persona,
        global_banner_bytes=global_banner_bytes,
    )

    await synchronizer.synchronize()

    assert user.edits == [
        {"avatar": loaded_persona.avatar_bytes},
        {"banner": global_banner_bytes},
    ]


@pytest.mark.asyncio
async def test_banner_failure_does_not_block_other_fields_and_retries(
    loaded_persona,
    global_banner_bytes,
) -> None:
    user = FakeClientUser("Old Name", field_failures={"banner": 1})
    synchronizer = DiscordProfileSynchronizer(
        SimpleNamespace(user=user, guilds=[]),
        loaded_persona,
        global_banner_bytes=global_banner_bytes,
    )

    await synchronizer.synchronize()
    assert synchronizer.global_profile_synchronized is False

    await synchronizer.synchronize()

    assert synchronizer.global_profile_synchronized is True
    assert user.edits == [
        {"username": "Walter Vigil"},
        {"avatar": loaded_persona.avatar_bytes},
        {"banner": global_banner_bytes},
        {"banner": global_banner_bytes},
    ]


@pytest.mark.asyncio
async def test_guild_bio_syncs_once_without_managing_null_nickname(
    loaded_persona,
    global_banner_bytes,
) -> None:
    member = FakeMember()
    guild = SimpleNamespace(id=123, me=member)
    user = FakeClientUser(loaded_persona.profile.discord.username)
    synchronizer = DiscordProfileSynchronizer(
        SimpleNamespace(user=user, guilds=[guild]),
        loaded_persona,
        global_banner_bytes=global_banner_bytes,
    )

    await synchronizer.synchronize()
    await synchronizer.synchronize()

    assert member.edits == [{"bio": loaded_persona.profile.discord.bio}]


@pytest.mark.asyncio
async def test_guild_profile_sync_includes_configured_nickname(
    loaded_persona,
    global_banner_bytes,
) -> None:
    discord_profile = loaded_persona.profile.discord.model_copy(
        update={"nickname": "Sgt. Vigil"},
    )
    profile = loaded_persona.profile.model_copy(update={"discord": discord_profile})
    persona = replace(loaded_persona, profile=profile)
    member = FakeMember()
    guild = SimpleNamespace(id=123, me=member)
    user = FakeClientUser(profile.discord.username)
    synchronizer = DiscordProfileSynchronizer(
        SimpleNamespace(user=user, guilds=[guild]),
        persona,
        global_banner_bytes=global_banner_bytes,
    )

    await synchronizer.synchronize()

    assert member.edits == [
        {"nick": "Sgt. Vigil", "bio": profile.discord.bio},
    ]


@pytest.mark.asyncio
async def test_guild_profile_sync_retries_after_failure(
    loaded_persona,
    global_banner_bytes,
) -> None:
    member = FakeMember(failures=1)
    guild = SimpleNamespace(id=123, me=member)
    user = FakeClientUser(loaded_persona.profile.discord.username)
    synchronizer = DiscordProfileSynchronizer(
        SimpleNamespace(user=user, guilds=[guild]),
        loaded_persona,
        global_banner_bytes=global_banner_bytes,
    )

    await synchronizer.synchronize()
    await synchronizer.synchronize()

    assert member.edits == [
        {"bio": loaded_persona.profile.discord.bio},
        {"bio": loaded_persona.profile.discord.bio},
    ]
