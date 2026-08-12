from __future__ import annotations

from types import SimpleNamespace

import discord
import pytest

from arrest_report_agent.operational_profiles.discord import CallsignCog, CallsignModal


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

    modal = CallsignModal(cog, requester_id=11, current="242WS")

    assert modal.value.value == "242WS"
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

    await cog.save(interaction, requester_id=11, callsign="242WS")

    assert saved == [(1, 11, "242WS")]
    assert messages[0]["ephemeral"] is True
    assert messages[0]["embed"].title == persona.messages.interface.callsigns.saved_title
    assert "242WS" in messages[0]["embed"].description


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

    await cog.save(interaction, requester_id=11, callsign="242WS")

    assert messages[0]["ephemeral"] is True
    assert persona.messages.interface.callsigns.unauthorized in messages[0]["embed"].description
