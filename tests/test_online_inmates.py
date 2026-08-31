from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord
import pytest

from arrest_report_agent.infrastructure.sarp import SarpOnlineCharacter
from arrest_report_agent.online_inmates import (
    OnlineInmateCog,
    OnlineInmateService,
    OnlineInmateSnapshot,
    OnlineInmateView,
    build_online_inmates_embed,
)


def _character(
    character_id: int,
    name: str,
    *,
    jailed: int,
) -> SarpOnlineCharacter:
    return SarpOnlineCharacter.model_validate(
        {
            "character_id": character_id,
            "character_name": name,
            "character_level": 10,
            "character_jailed": jailed,
            "account_id": character_id + 100,
            "account_name": f"account-{character_id}",
        }
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


async def test_online_inmate_service_filters_sorts_and_caches_snapshot() -> None:
    calls = 0

    async def get_online_characters() -> list[SarpOnlineCharacter]:
        nonlocal calls
        calls += 1
        return [
            _character(1, "Zoe_Alvarez", jailed=1),
            _character(2, "Aaron_Wilson", jailed=0),
            _character(3, "Álvaro_Mendez", jailed=1),
        ]

    service = OnlineInmateService(
        SimpleNamespace(get_online_characters=get_online_characters),
        cache_seconds=30,
    )

    first, second = await asyncio.gather(service.query(), service.query())

    assert first == second
    assert first.server_connected_count == 3
    assert first.inmate_names == ("Álvaro Mendez", "Zoe Alvarez")
    assert calls == 1


def test_online_inmate_service_rejects_negative_cache_duration() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        OnlineInmateService(SimpleNamespace(), cache_seconds=-1)


def test_online_inmate_embed_is_compact_and_paginates_names_only() -> None:
    snapshot = OnlineInmateSnapshot(
        server_connected_count=70,
        inmate_names=tuple(f"Interno {index:02d}" for index in range(23)),
    )

    first = build_online_inmates_embed(snapshot, page=0, page_size=10)
    last = build_online_inmates_embed(snapshot, page=2, page_size=10)

    assert first.title == "⛓️ Internos conectados"
    assert "**Servidor:** 70 conectados · **En prisión:** 23" in first.description
    assert "Interno 00" in first.description
    assert "Interno 09" in first.description
    assert "Interno 10" not in first.description
    assert first.footer.text == "Página 1/3"
    assert "Interno 20\nInterno 21\nInterno 22" in last.description
    assert last.footer.text == "Página 3/3"
    assert "nivel" not in first.description.casefold()
    assert "cuenta" not in first.description.casefold()
    assert "cach" not in first.description.casefold()


def test_online_inmate_embed_handles_empty_snapshot_without_cache_details() -> None:
    embed = build_online_inmates_embed(
        OnlineInmateSnapshot(server_connected_count=42, inmate_names=()),
    )

    assert embed.description == (
        "**Servidor:** 42 conectados · **En prisión:** 0\n\n"
        "Actualmente no hay personajes conectados en prisión."
    )
    assert embed.footer is None


async def test_online_inmate_view_keeps_one_snapshot_for_every_page() -> None:
    snapshot = OnlineInmateSnapshot(
        server_connected_count=70,
        inmate_names=tuple(f"Interno {index:02d}" for index in range(12)),
    )
    view = OnlineInmateView(99, snapshot, page_size=5)

    assert view.page_count == 3
    assert view.previous.disabled is True
    assert view.next.disabled is False


async def test_online_inmate_command_rejects_non_operational_member(persona) -> None:
    responses: list[dict[str, object]] = []
    member = _member(11, (999,))

    async def cached(_guild_id: int):
        return SimpleNamespace(
            settings=SimpleNamespace(operational_staff_role_ids=(200,)),
            persona=SimpleNamespace(profile=persona),
        )

    async def respond(**kwargs: object) -> None:
        responses.append(kwargs)

    async def query() -> OnlineInmateSnapshot:
        raise AssertionError("unauthorized users must not reach SARP")

    cog = OnlineInmateCog(
        SimpleNamespace(cached=cached),
        SimpleNamespace(query=query),
        persona,
    )
    context = SimpleNamespace(guild=member.guild, author=member, respond=respond)

    await OnlineInmateCog.online_inmates.callback(cog, context)  # type: ignore[arg-type]

    assert responses[0]["ephemeral"] is True
    assert responses[0]["embed"].description == persona.messages.interface.faction_status.unauthorized


async def test_online_inmate_command_publishes_public_paginated_snapshot(persona) -> None:
    deferred: list[dict[str, object]] = []
    followups: list[dict[str, object]] = []
    member = _member(11, (200,))

    async def cached(_guild_id: int):
        return SimpleNamespace(
            settings=SimpleNamespace(operational_staff_role_ids=(200,)),
            persona=SimpleNamespace(profile=persona),
        )

    async def defer(**kwargs: object) -> None:
        deferred.append(kwargs)

    async def send(**kwargs: object) -> None:
        followups.append(kwargs)

    async def query() -> OnlineInmateSnapshot:
        return OnlineInmateSnapshot(
            server_connected_count=70,
            inmate_names=tuple(f"Interno {index:02d}" for index in range(12)),
        )

    cog = OnlineInmateCog(
        SimpleNamespace(cached=cached),
        SimpleNamespace(query=query),
        persona,
        page_size=10,
    )
    context = SimpleNamespace(
        guild=member.guild,
        author=member,
        defer=defer,
        followup=SimpleNamespace(send=send),
    )

    await OnlineInmateCog.online_inmates.callback(cog, context)  # type: ignore[arg-type]

    assert deferred == [{"ephemeral": False}]
    assert followups[0]["ephemeral"] is False
    assert isinstance(followups[0]["view"], OnlineInmateView)
    assert followups[0]["allowed_mentions"].everyone is False
