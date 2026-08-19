from __future__ import annotations

from types import SimpleNamespace

import discord

from arrest_report_agent.config import OperationalResourcesOptions
from arrest_report_agent.faction_status import FactionPresenceMember, FactionPresenceSnapshot
from arrest_report_agent.infrastructure.sarp import SarpServerCacheInfo
from arrest_report_agent.infrastructure.sarp.errors import SarpTransportError
from arrest_report_agent.operational_resources import (
    OperationalResourcesCog,
    OperationalResourcesService,
    OperationalResourcesSnapshot,
    build_operational_resources_embed,
)


def _options() -> OperationalResourcesOptions:
    return OperationalResourcesOptions(
        faction_id=3,
        game_server_address="play.sarp.es:7777",
        pcu_url="https://ucp.sarp.es/app/ucp/home",
        forum_url="https://forum.sarp.es/lssd",
        manual_url="https://docs.google.com/document/d/manual/edit",
        penal_code_url="https://ucp.sarp.es/app/ucp/penalcode",
    )


def _member(user_id: int, role_ids: tuple[int, ...]) -> discord.Member:
    roles = {role_id: SimpleNamespace(id=role_id) for role_id in role_ids}
    guild = SimpleNamespace(id=1, icon=None, get_role=roles.get, default_role=None)
    state = SimpleNamespace(store_user=lambda data: SimpleNamespace(id=int(data["id"])))
    return discord.Member(
        data={"user": {"id": str(user_id)}, "roles": [str(role_id) for role_id in role_ids]},
        guild=guild,
        state=state,
    )


def _status() -> FactionPresenceSnapshot:
    return FactionPresenceSnapshot(
        faction_id=3,
        total_members=60,
        connected_members=(
            FactionPresenceMember("Ana Deputy", on_duty=True),
            FactionPresenceMember("Ben Deputy", on_duty=True),
            FactionPresenceMember("Cal Deputy", on_duty=False),
        ),
        server_connected_count=39,
    )


def _cache() -> SarpServerCacheInfo:
    return SarpServerCacheInfo.model_validate(
        {
            "exists": True,
            "filename": "cache.zip",
            "url": "https://cdn.example.com/cache.zip?v=27",
            "sizeBytes": 822_146_470,
            "lastModified": "2026-08-19T04:03:12Z",
            "version": 27,
        }
    )


async def test_resources_service_reuses_cache_artifact_and_faction_service() -> None:
    faction_calls: list[int] = []
    cache_calls = 0

    async def query(faction_id: int) -> FactionPresenceSnapshot:
        faction_calls.append(faction_id)
        return _status()

    async def get_server_cache_info() -> SarpServerCacheInfo:
        nonlocal cache_calls
        cache_calls += 1
        return _cache()

    service = OperationalResourcesService(
        SimpleNamespace(query=query),
        SimpleNamespace(get_server_cache_info=get_server_cache_info),
    )

    first = await service.query(3)
    second = await service.query(3)

    assert first.server_cache == _cache()
    assert second == first
    assert faction_calls == [3, 3]
    assert cache_calls == 1


async def test_resources_service_omits_cache_when_only_optional_call_fails() -> None:
    async def query(_faction_id: int) -> FactionPresenceSnapshot:
        return _status()

    async def get_server_cache_info() -> SarpServerCacheInfo:
        raise SarpTransportError("GET", "/public/server-cache/download")

    service = OperationalResourcesService(
        SimpleNamespace(query=query),
        SimpleNamespace(get_server_cache_info=get_server_cache_info),
    )

    snapshot = await service.query(3)

    assert snapshot.faction_status == _status()
    assert snapshot.server_cache is None


def test_resources_embed_is_nested_bounded_and_uses_dynamic_cache(persona) -> None:
    snapshot = OperationalResourcesSnapshot(_status(), _cache())

    embed = build_operational_resources_embed(
        snapshot,
        _options(),
        teamspeak_address="51.81.49.72:11189",
        organization_acronym="LSSD",
        ui=persona.messages.interface.operational_resources,
        thumbnail_url="https://cdn.discordapp.com/icons/1/icon.png",
    )

    assert embed.title == "📡 Recursos y estado operativo"
    assert embed.fields[0].name == "📊 Estado actual"
    assert embed.fields[0].value.startswith("└─ 🌐 **Servidor:** 39")
    assert "🟢 **Facción:** 3" in embed.fields[0].value
    assert "🚓 **Servicio:** 2" in embed.fields[0].value
    assert "📈 **Actividad:** 67%" in embed.fields[0].value
    assert "└─ **Servidor:** `play.sarp.es:7777`" in embed.fields[1].value
    assert "└─ [PCU](https://ucp.sarp.es/app/ucp/home)" in embed.fields[2].value
    assert "v=27" in embed.fields[3].value
    assert "**Versión:** 27" in embed.fields[3].value
    assert "**Tamaño:** 784 MiB" in embed.fields[3].value
    assert "/conectados" in embed.fields[4].value
    assert embed.thumbnail.url == "https://cdn.discordapp.com/icons/1/icon.png"


def test_resources_embed_omits_cache_section_when_api_is_unavailable(persona) -> None:
    embed = build_operational_resources_embed(
        OperationalResourcesSnapshot(_status(), None),
        _options(),
        teamspeak_address="51.81.49.72:11189",
        organization_acronym="LSSD",
        ui=persona.messages.interface.operational_resources,
    )

    assert all(field.name != "📦 Recursos del servidor" for field in embed.fields)
    assert embed.fields[-1].name == "👥 Detalle del personal"


async def test_resources_command_rejects_member_without_operational_role(persona) -> None:
    responses: list[dict[str, object]] = []
    author = _member(11, (999,))

    async def cached(_guild_id: int):
        return SimpleNamespace(
            settings=SimpleNamespace(
                operational_staff_role_ids=(200,),
                operational_resources_options=_options(),
            ),
            persona=SimpleNamespace(profile=persona),
        )

    async def respond(**kwargs: object) -> None:
        responses.append(kwargs)

    async def query(*_args: object) -> OperationalResourcesSnapshot:
        raise AssertionError("Unauthorized users must not reach SARP")

    cog = OperationalResourcesCog(
        SimpleNamespace(cached=cached),
        SimpleNamespace(query=query),
        persona,
    )
    context = SimpleNamespace(guild=author.guild, author=author, respond=respond)

    await OperationalResourcesCog.resources.callback(cog, context)  # type: ignore[arg-type]

    assert responses[0]["ephemeral"] is True
    assert responses[0]["embed"].description == (
        persona.messages.interface.operational_resources.unauthorized
    )


async def test_resources_command_publishes_for_operational_staff(persona) -> None:
    deferred: list[dict[str, object]] = []
    followups: list[dict[str, object]] = []
    author = _member(11, (200,))

    async def cached(_guild_id: int):
        return SimpleNamespace(
            settings=SimpleNamespace(
                operational_staff_role_ids=(200,),
                operational_resources_options=_options(),
                teamspeak_address="51.81.49.72:11189",
            ),
            persona=SimpleNamespace(profile=persona),
        )

    async def defer(**kwargs: object) -> None:
        deferred.append(kwargs)

    async def send(**kwargs: object) -> None:
        followups.append(kwargs)

    async def query(faction_id: int) -> OperationalResourcesSnapshot:
        assert faction_id == 3
        return OperationalResourcesSnapshot(_status(), _cache())

    cog = OperationalResourcesCog(
        SimpleNamespace(cached=cached),
        SimpleNamespace(query=query),
        persona,
    )
    context = SimpleNamespace(
        guild=author.guild,
        author=author,
        defer=defer,
        followup=SimpleNamespace(send=send),
    )

    await OperationalResourcesCog.resources.callback(cog, context)  # type: ignore[arg-type]

    assert deferred == [{"ephemeral": False}]
    assert followups[0]["ephemeral"] is False
    assert followups[0]["embed"].title == "📡 Recursos y estado operativo"
    assert followups[0]["allowed_mentions"].everyone is False
