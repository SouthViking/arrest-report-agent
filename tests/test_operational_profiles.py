from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import discord
import pytest

from arrest_report_agent.operational_profiles.discord import (
    CallsignCog,
    CallsignDirectoryView,
    CallsignModal,
    _directory_groups,
)
from arrest_report_agent.operational_profiles.models import (
    CallsignFormatError,
    MemberProfile,
    normalize_callsign,
)


def _member(user_id: int, role_ids: tuple[int, ...]) -> discord.Member:
    roles = {role_id: SimpleNamespace(id=role_id) for role_id in role_ids}
    guild = SimpleNamespace(id=1, get_role=roles.get, default_role=None)
    state = SimpleNamespace(store_user=lambda data: SimpleNamespace(id=int(data["id"])))
    return discord.Member(
        data={"user": {"id": str(user_id)}, "roles": [str(role_id) for role_id in role_ids]},
        guild=guild,
        state=state,
    )


@pytest.mark.asyncio
async def test_callsign_modal_prefills_existing_value(persona) -> None:
    cog = CallsignCog(SimpleNamespace(), SimpleNamespace(), persona)

    modal = CallsignModal(cog, requester_id=11, current="280L")

    assert modal.value.value == "280L"
    assert modal.custom_id == "callsign:v1:update:11"


@pytest.mark.asyncio
async def test_callsign_confirmation_is_ephemeral_and_personality_aware(persona) -> None:
    messages: list[dict[str, object]] = []
    saved: list[tuple[int, int, str]] = []
    member = _member(11, (200,))

    async def cached(_guild_id: int):
        return SimpleNamespace(
            settings=SimpleNamespace(operational_staff_role_ids=(200,)),
            persona=SimpleNamespace(profile=persona),
        )

    async def upsert_callsign(guild_id: int, user_id: int, callsign: str):
        saved.append((guild_id, user_id, callsign))
        return SimpleNamespace(callsign=callsign)

    async def send_message(**kwargs: object) -> None:
        messages.append(kwargs)

    cog = CallsignCog(
        SimpleNamespace(cached=cached),
        SimpleNamespace(upsert_callsign=upsert_callsign),
        persona,
    )
    interaction = SimpleNamespace(
        guild=member.guild,
        user=member,
        response=SimpleNamespace(send_message=send_message),
    )

    await cog.save(interaction, requester_id=11, callsign="280l")

    assert saved == [(1, 11, "280L")]
    assert messages[0]["ephemeral"] is True
    assert messages[0]["embed"].title == persona.messages.interface.callsigns.saved_title
    assert "280L" in messages[0]["embed"].description


@pytest.mark.parametrize(
    ("raw", "expected"),
    (("60f", "60F"), (" 280l ", "280L"), ("282B", "282B"), ("281s", "281S")),
)
def test_callsign_format_is_normalized(raw: str, expected: str) -> None:
    assert normalize_callsign(raw) == expected


@pytest.mark.parametrize("raw", ("6L", "280", "280WS", "ABCL", "280X", "280 L", "1234L"))
def test_callsign_format_rejects_unsupported_values(raw: str) -> None:
    with pytest.raises(CallsignFormatError):
        normalize_callsign(raw)


@pytest.mark.asyncio
async def test_callsign_invalid_format_uses_personality_error(persona) -> None:
    messages: list[dict[str, object]] = []
    member = _member(11, (200,))

    async def cached(_guild_id: int):
        return SimpleNamespace(
            settings=SimpleNamespace(operational_staff_role_ids=(200,)),
            persona=SimpleNamespace(profile=persona),
        )

    async def upsert_callsign(*_args):
        raise CallsignFormatError

    async def send_message(**kwargs: object) -> None:
        messages.append(kwargs)

    cog = CallsignCog(
        SimpleNamespace(cached=cached),
        SimpleNamespace(upsert_callsign=upsert_callsign),
        persona,
    )
    interaction = SimpleNamespace(
        guild=member.guild,
        user=member,
        response=SimpleNamespace(send_message=send_message),
    )

    await cog.save(interaction, requester_id=11, callsign="troleo")

    assert messages[0]["ephemeral"] is True
    assert messages[0]["embed"].description == persona.messages.interface.callsigns.invalid_format


@pytest.mark.asyncio
async def test_callsign_rejects_members_without_operational_role(persona) -> None:
    messages: list[dict[str, object]] = []
    member = _member(11, (999,))

    async def cached(_guild_id: int):
        return SimpleNamespace(
            settings=SimpleNamespace(operational_staff_role_ids=(200,)),
            persona=SimpleNamespace(profile=persona),
        )

    async def send_message(**kwargs: object) -> None:
        messages.append(kwargs)

    cog = CallsignCog(SimpleNamespace(cached=cached), SimpleNamespace(), persona)
    interaction = SimpleNamespace(
        guild=member.guild,
        user=member,
        response=SimpleNamespace(send_message=send_message),
    )

    await cog.save(interaction, requester_id=11, callsign="280L")

    assert messages[0]["ephemeral"] is True
    assert persona.messages.interface.callsigns.unauthorized in messages[0]["embed"].description


@pytest.mark.asyncio
async def test_callsign_directory_requires_operational_role(persona) -> None:
    responses: list[dict[str, object]] = []
    member = _member(11, (999,))

    async def cached(_guild_id: int):
        return SimpleNamespace(
            settings=SimpleNamespace(operational_staff_role_ids=(200,)),
            persona=SimpleNamespace(profile=persona),
        )

    async def list_for_guild(_guild_id: int):
        raise AssertionError("unauthorized members must not query the directory")

    async def respond(**kwargs: object) -> None:
        responses.append(kwargs)

    cog = CallsignCog(
        SimpleNamespace(cached=cached),
        SimpleNamespace(list_for_guild=list_for_guild),
        persona,
    )
    context = SimpleNamespace(guild=member.guild, author=member, respond=respond)

    await CallsignCog.callsigns.callback(cog, context)  # type: ignore[arg-type]

    assert responses[0]["ephemeral"] is True
    assert persona.messages.interface.callsigns.unauthorized in responses[0]["embed"].description


@pytest.mark.asyncio
async def test_callsign_directory_is_public_without_notifications(persona) -> None:
    now = datetime.now(UTC)
    responses: list[dict[str, object]] = []
    member = _member(11, (200,))
    directory_member = SimpleNamespace(id=11, display_name="Member 11")
    member.guild.get_member = lambda user_id: directory_member if user_id == member.id else None
    profile = MemberProfile(
        guild_id=1,
        user_id=11,
        callsign="280L",
        created_at=now,
        updated_at=now,
    )

    async def cached(_guild_id: int):
        return SimpleNamespace(
            settings=SimpleNamespace(operational_staff_role_ids=(200,)),
            persona=SimpleNamespace(profile=persona),
        )

    async def list_for_guild(_guild_id: int):
        return (profile,)

    async def respond(**kwargs: object) -> None:
        responses.append(kwargs)

    cog = CallsignCog(
        SimpleNamespace(cached=cached),
        SimpleNamespace(list_for_guild=list_for_guild),
        persona,
    )
    context = SimpleNamespace(guild=member.guild, author=member, respond=respond)

    await CallsignCog.callsigns.callback(cog, context)  # type: ignore[arg-type]

    assert responses[0]["ephemeral"] is False
    assert responses[0]["view"] is None
    assert responses[0]["allowed_mentions"].everyone is False
    assert responses[0]["embed"].fields[0].name == "280L"


def test_callsign_commands_are_root_commands() -> None:
    commands = (CallsignCog.callsign, CallsignCog.callsigns, CallsignCog.info_callsigns)

    assert [command.name for command in commands] == ["callsign", "callsigns", "info-callsigns"]
    assert all(command.parent is None for command in commands)


def test_callsign_directory_groups_current_members_case_insensitively() -> None:
    now = datetime.now(UTC)
    members = {
        11: SimpleNamespace(id=11, display_name="Zulu"),
        12: SimpleNamespace(id=12, display_name="Alpha"),
        13: SimpleNamespace(id=13, display_name="Bravo"),
    }
    guild = SimpleNamespace(get_member=members.get)
    profiles = tuple(
        MemberProfile(
            guild_id=1,
            user_id=user_id,
            callsign=callsign,
            created_at=now,
            updated_at=now,
        )
        for user_id, callsign in (
            (11, "10-L"),
            (12, "10-l"),
            (13, "2-L"),
            (99, "1-L"),
        )
    )

    groups = _directory_groups(guild, profiles)  # type: ignore[arg-type]

    assert [(group.callsign, group.user_ids) for group in groups] == [
        ("2-L", (13,)),
        ("10-L", (12, 11)),
    ]


@pytest.mark.asyncio
async def test_callsign_directory_embed_is_grouped_and_does_not_expose_stale_members(
    persona,
) -> None:
    now = datetime.now(UTC)
    members = {
        user_id: SimpleNamespace(id=user_id, display_name=f"Member {user_id}")
        for user_id in range(1, 11)
    }
    guild = SimpleNamespace(get_member=members.get)
    profiles = tuple(
        MemberProfile(
            guild_id=1,
            user_id=user_id,
            callsign=f"{user_id}-L",
            created_at=now,
            updated_at=now,
        )
        for user_id in range(1, 11)
    )
    groups = _directory_groups(guild, profiles)  # type: ignore[arg-type]
    view = CallsignDirectoryView(1, groups, persona.messages.interface.callsigns)

    first = view.render()
    view.page = 1
    view._sync_buttons()
    second = view.render()

    assert len(first.fields) == 8
    assert len(second.fields) == 2
    assert first.footer.text == "Página 1/2 · 10 callsigns · 10 miembros"
    assert second.footer.text == "Página 2/2 · 10 callsigns · 10 miembros"


@pytest.mark.asyncio
async def test_info_callsigns_is_public_and_personality_aware(persona) -> None:
    responses: list[dict[str, object]] = []

    async def cached(_guild_id: int):
        return SimpleNamespace(persona=SimpleNamespace(profile=persona))

    async def respond(**kwargs: object) -> None:
        responses.append(kwargs)

    cog = CallsignCog(SimpleNamespace(cached=cached), SimpleNamespace(), persona)
    context = SimpleNamespace(
        guild=SimpleNamespace(id=1),
        author=SimpleNamespace(id=11),
        respond=respond,
    )

    await CallsignCog.info_callsigns.callback(cog, context)  # type: ignore[arg-type]

    assert responses[0]["ephemeral"] is False
    assert responses[0]["embed"].description == persona.messages.interface.callsigns.info_intro
    assert "280L" in responses[0]["embed"].fields[0].value
    assert responses[0]["embed"].fields[1].name == "Uso en pagers"
    assert "Responder / actualizar" in responses[0]["embed"].fields[1].value
    assert "automáticamente" in responses[0]["embed"].fields[1].value
