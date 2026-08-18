from __future__ import annotations

from types import SimpleNamespace

import discord

from arrest_report_agent.faction_status import (
    FactionPresenceMember,
    FactionPresenceSnapshot,
    FactionStatusCog,
    FactionStatusService,
    build_faction_status_embed,
)
from arrest_report_agent.infrastructure.sarp import SarpFactionDetail, SarpFactionRank
from arrest_report_agent.infrastructure.sarp.errors import SarpTransportError


def _member(user_id: int, role_ids: tuple[int, ...]) -> discord.Member:
    roles = {role_id: SimpleNamespace(id=role_id) for role_id in role_ids}
    guild = SimpleNamespace(id=1, get_role=roles.get, default_role=None)
    state = SimpleNamespace(store_user=lambda data: SimpleNamespace(id=int(data["id"])))
    return discord.Member(
        data={"user": {"id": str(user_id)}, "roles": [str(role_id) for role_id in role_ids]},
        guild=guild,
        state=state,
    )


def _detail() -> SarpFactionDetail:
    return SarpFactionDetail.model_validate(
        {
            "factionMembers": [
                {
                    "id": 1,
                    "name": "Zachary_Wilcox",
                    "online": 1,
                    "factionduty": 0,
                    "rank": 3,
                },
                {
                    "id": 2,
                    "name": "Álvaro_Mendez",
                    "online": 1,
                    "factionduty": 1,
                    "rank": 1,
                },
                {
                    "id": 3,
                    "name": "Bruno_Alvarez",
                    "online": 0,
                    "factionduty": 1,
                    "rank": 2,
                },
            ]
        }
    )


def _ranks() -> list[SarpFactionRank]:
    names = ("Employee", "Deputy Sheriff", "Sheriff's Sergeant", "Sheriff's Lieutenant")
    return [
        SarpFactionRank(
            id=20 + index,
            faction=3,
            name=name,
            member_tier=0,
            paycheck=300,
        )
        for index, name in enumerate(names)
    ]


async def test_faction_status_service_projects_both_states_and_coalesces_cache() -> None:
    detail_calls: list[int] = []
    rank_calls: list[int] = []

    async def get_faction_detail(faction_id: int) -> SarpFactionDetail:
        detail_calls.append(faction_id)
        return _detail()

    async def get_faction_ranks(faction_id: int) -> list[SarpFactionRank]:
        rank_calls.append(faction_id)
        return _ranks()

    service = FactionStatusService(
        SimpleNamespace(
            get_faction_detail=get_faction_detail,
            get_faction_ranks=get_faction_ranks,
        ),
        cache_seconds=30,
        rank_cache_seconds=300,
    )

    first = await service.query(3)
    second = await service.query(3)

    assert first.connected_members == (
        FactionPresenceMember(
            "Zachary Wilcox",
            on_duty=False,
            rank_name="Sheriff's Lieutenant",
            rank_order=3,
        ),
        FactionPresenceMember(
            "Álvaro Mendez",
            on_duty=True,
            rank_name="Deputy Sheriff",
            rank_order=1,
        ),
    )
    assert first.connected_count == 2
    assert first.on_duty_count == 1
    assert first.duty_percentage == 50
    assert first.total_members == 3
    assert second == first
    assert detail_calls == [3]
    assert rank_calls == [3]


async def test_faction_rank_cache_outlives_the_live_roster_cache() -> None:
    detail_calls = 0
    rank_calls = 0

    async def get_faction_detail(_faction_id: int) -> SarpFactionDetail:
        nonlocal detail_calls
        detail_calls += 1
        return _detail()

    async def get_faction_ranks(_faction_id: int) -> list[SarpFactionRank]:
        nonlocal rank_calls
        rank_calls += 1
        return _ranks()

    service = FactionStatusService(
        SimpleNamespace(
            get_faction_detail=get_faction_detail,
            get_faction_ranks=get_faction_ranks,
        ),
        cache_seconds=0,
        rank_cache_seconds=300,
    )

    await service.query(3)
    await service.query(3)

    assert detail_calls == 2
    assert rank_calls == 1


async def test_faction_status_keeps_working_when_rank_catalog_is_unavailable() -> None:
    async def get_faction_detail(_faction_id: int) -> SarpFactionDetail:
        return _detail()

    async def get_faction_ranks(_faction_id: int) -> list[SarpFactionRank]:
        raise SarpTransportError("GET", "/my-faction/ranks/3")

    service = FactionStatusService(
        SimpleNamespace(
            get_faction_detail=get_faction_detail,
            get_faction_ranks=get_faction_ranks,
        ),
    )

    snapshot = await service.query(3)

    assert snapshot.connected_count == 2
    assert all(member.rank_name == "Rango desconocido" for member in snapshot.connected_members)


def test_faction_status_embed_is_bounded_and_reports_remaining_members(persona) -> None:
    snapshot = FactionPresenceSnapshot(
        faction_id=3,
        total_members=30,
        connected_members=tuple(
            FactionPresenceMember(f"Miembro {index:02d}", on_duty=index % 2 == 0)
            for index in range(25)
        ),
    )

    embed = build_faction_status_embed(
        snapshot,
        persona.messages.interface.faction_status,
    )

    assert embed.title == "👥 Estado de la facción"
    assert [field.name for field in embed.fields[:3]] == [
        "Conectados",
        "En servicio",
        "Actividad operativa",
    ]
    assert embed.fields[0].value == "**25**"
    assert embed.fields[1].value == "**13**"
    assert embed.fields[2].value == "**52%**"
    assert embed.fields[3].name == "🟢 En servicio · 13"
    assert len(embed.fields[3].value.splitlines()) == 11
    assert embed.fields[3].value.startswith(
        "Rango desconocido · Miembro 00\nRango desconocido · Miembro 02"
    )
    assert embed.fields[3].value.endswith("… y 3 más.")
    assert embed.fields[4].name == "▫️ Conectados, fuera de servicio · 12"
    assert len(embed.fields[4].value.splitlines()) == 11
    assert embed.fields[4].value.startswith(
        "Rango desconocido · Miembro 01\nRango desconocido · Miembro 03"
    )
    assert embed.fields[4].value.endswith("… y 2 más.")


def test_faction_status_embed_handles_empty_state_without_dividing_by_zero(persona) -> None:
    snapshot = FactionPresenceSnapshot(
        faction_id=3,
        total_members=74,
        connected_members=(),
    )

    embed = build_faction_status_embed(
        snapshot,
        persona.messages.interface.faction_status,
    )

    assert embed.title == "👥 Estado de la facción"
    assert embed.description == persona.messages.interface.faction_status.no_connected
    assert embed.fields[0].value == "**0**"
    assert embed.fields[1].value == "**0**"
    assert embed.fields[2].value == "**0%**"


async def test_connected_command_rejects_non_operational_member(persona) -> None:
    responses: list[dict[str, object]] = []
    member = _member(11, (999,))

    async def cached(_guild_id: int):
        return SimpleNamespace(
            settings=SimpleNamespace(
                operational_staff_role_ids=(200,),
                faction_status_faction_id=3,
            ),
            persona=SimpleNamespace(profile=persona),
        )

    async def respond(**kwargs: object) -> None:
        responses.append(kwargs)

    async def query(*_args: object) -> FactionPresenceSnapshot:
        raise AssertionError("unauthorized users must not reach SARP")

    cog = FactionStatusCog(
        SimpleNamespace(cached=cached),
        SimpleNamespace(query=query),
        persona,
    )
    context = SimpleNamespace(guild=member.guild, author=member, respond=respond)

    await FactionStatusCog.connected_members.callback(cog, context)  # type: ignore[arg-type]

    assert responses[0]["ephemeral"] is True
    assert responses[0]["embed"].description == (
        persona.messages.interface.faction_status.unauthorized
    )


async def test_connected_command_publishes_summary_view(persona) -> None:
    deferred: list[dict[str, object]] = []
    followups: list[dict[str, object]] = []
    member = _member(11, (200,))

    async def cached(_guild_id: int):
        return SimpleNamespace(
            settings=SimpleNamespace(
                operational_staff_role_ids=(200,),
                faction_status_faction_id=3,
            ),
            persona=SimpleNamespace(profile=persona),
        )

    async def defer(**kwargs: object) -> None:
        deferred.append(kwargs)

    async def send(**kwargs: object) -> None:
        followups.append(kwargs)

    async def query(
        faction_id: int,
    ) -> FactionPresenceSnapshot:
        assert faction_id == 3
        return FactionPresenceSnapshot(
            3,
            74,
            (FactionPresenceMember("Adriel Beltran", on_duty=True),),
        )

    cog = FactionStatusCog(
        SimpleNamespace(cached=cached),
        SimpleNamespace(query=query),
        persona,
    )
    context = SimpleNamespace(
        guild=member.guild,
        author=member,
        defer=defer,
        followup=SimpleNamespace(send=send),
    )

    await FactionStatusCog.connected_members.callback(cog, context)  # type: ignore[arg-type]

    assert deferred == [{"ephemeral": False}]
    assert followups[0]["ephemeral"] is False
    assert followups[0]["embed"].title == "👥 Estado de la facción"
    assert followups[0]["allowed_mentions"].everyone is False
