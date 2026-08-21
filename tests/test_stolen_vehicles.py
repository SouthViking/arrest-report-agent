from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from arrest_report_agent.config import AccessPolicyKey, AccessPolicyMode
from arrest_report_agent.infrastructure.sarp import SarpStolenVehicle, SarpStolenVehiclePage
from arrest_report_agent.stolen_vehicles import (
    StolenVehicleFeedWorker,
    StolenVehicleService,
    StolenVehicleStatusFilter,
    build_stolen_vehicle_alert_embed,
    build_stolen_vehicle_list_embed,
)
from arrest_report_agent.stolen_vehicles.models import StolenVehicleQueryResult
from arrest_report_agent.stolen_vehicles.repository import StolenVehicleFeedCursor


def stolen_vehicle(
    report_id: int,
    *,
    active: bool = True,
    created_minute: int | None = None,
) -> SarpStolenVehicle:
    return SarpStolenVehicle(
        id=report_id,
        owner_id=41,
        owner_name="Terry <@123> Davis",
        model="Sultan **RS**",
        plate=f"SV{report_id}",
        colors=("Negro", "Blanco"),
        description="Sustraído frente a **la estación** por <@456>.",
        is_active=active,
        created_at=datetime(
            2026,
            8,
            21,
            18,
            created_minute if created_minute is not None else report_id % 60,
            tzinfo=UTC,
        ),
    )


def page(*items: SarpStolenVehicle, total: int | None = None) -> SarpStolenVehiclePage:
    return SarpStolenVehiclePage(
        items=list(items),
        total=len(items) if total is None else total,
        page=1,
        limit=100,
        pages=1,
    )


class FakeSnapshot:
    guild_id = 123

    def __init__(self, *, enabled: bool = True, channel_ids: tuple[int, ...] = (456,)) -> None:
        self.features = {"stolen_vehicles": SimpleNamespace(enabled=enabled)}
        mode = AccessPolicyMode.RESTRICTED if channel_ids else AccessPolicyMode.DISABLED
        self.policies = {
            AccessPolicyKey.STOLEN_VEHICLE_CHANNELS: SimpleNamespace(
                mode=mode,
                ids=channel_ids,
            )
        }

    def policy(self, key: AccessPolicyKey) -> object:
        return self.policies[key]


class FakeProvider:
    def __init__(self, snapshot: FakeSnapshot) -> None:
        self.bundle = SimpleNamespace(snapshot=snapshot)

    async def cached(self, _guild_id: int) -> object:
        return self.bundle


class FakeRepository:
    def __init__(self) -> None:
        self.values: dict[tuple[int, int], int] = {}
        self.advances: list[int] = []

    async def get(self, guild_id: int, channel_id: int) -> StolenVehicleFeedCursor | None:
        value = self.values.get((guild_id, channel_id))
        return (
            StolenVehicleFeedCursor(guild_id, channel_id, value)
            if value is not None
            else None
        )

    async def initialize(self, guild_id: int, channel_id: int, report_id: int) -> bool:
        key = (guild_id, channel_id)
        if key in self.values:
            return False
        self.values[key] = report_id
        return True

    async def advance(self, guild_id: int, channel_id: int, report_id: int) -> None:
        self.values[(guild_id, channel_id)] = report_id
        self.advances.append(report_id)


class FakeService:
    def __init__(self, pages: list[SarpStolenVehiclePage]) -> None:
        self.pages = pages
        self.calls = 0

    async def list_page(
        self,
        *,
        page: int,
        limit: int,
        status: StolenVehicleStatusFilter,
    ) -> SarpStolenVehiclePage:
        assert page >= 1
        assert limit == 100
        assert status is StolenVehicleStatusFilter.ACTIVE
        self.calls += 1
        return self.pages.pop(0)


class FakeChannel:
    def __init__(self) -> None:
        self.embeds: list[discord.Embed] = []

    async def send(self, *, embed: discord.Embed, allowed_mentions: object) -> object:
        assert isinstance(allowed_mentions, discord.AllowedMentions)
        self.embeds.append(embed)
        return SimpleNamespace(id=9_000 + len(self.embeds))


async def test_feed_does_not_call_sarp_without_configured_channels() -> None:
    service = FakeService([page(stolen_vehicle(10))])
    worker = StolenVehicleFeedWorker(
        SimpleNamespace(is_ready=lambda: True),  # type: ignore[arg-type]
        FakeProvider(FakeSnapshot(enabled=False, channel_ids=())),  # type: ignore[arg-type]
        FakeRepository(),  # type: ignore[arg-type]
        service,  # type: ignore[arg-type]
        poll_seconds=60,
    )

    worker.start()
    await worker.process_once()

    assert worker.running is False
    assert service.calls == 0


async def test_first_sync_initializes_cursor_without_replaying_history() -> None:
    repository = FakeRepository()
    service = FakeService([page(stolen_vehicle(12), stolen_vehicle(11))])
    channel = FakeChannel()
    worker = StolenVehicleFeedWorker(
        SimpleNamespace(is_ready=lambda: True),  # type: ignore[arg-type]
        FakeProvider(FakeSnapshot()),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        service,  # type: ignore[arg-type]
        poll_seconds=60,
        enabled_guild_ids=(123,),
    )
    worker._resolve_channel = AsyncMock(return_value=channel)  # type: ignore[method-assign]

    await worker.process_once()

    assert repository.values[(123, 456)] == 12
    assert repository.advances == []
    assert channel.embeds == []


async def test_empty_first_sync_uses_zero_and_delivers_first_future_report() -> None:
    repository = FakeRepository()
    service = FakeService([page(), page(stolen_vehicle(1))])
    channel = FakeChannel()
    worker = StolenVehicleFeedWorker(
        SimpleNamespace(is_ready=lambda: True),  # type: ignore[arg-type]
        FakeProvider(FakeSnapshot()),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        service,  # type: ignore[arg-type]
        poll_seconds=60,
        enabled_guild_ids=(123,),
    )
    worker._resolve_channel = AsyncMock(return_value=channel)  # type: ignore[method-assign]

    await worker.process_once()
    await worker.process_once()

    assert repository.values[(123, 456)] == 1
    assert repository.advances == [1]
    assert len(channel.embeds) == 1


async def test_feed_delivers_only_new_reports_oldest_first() -> None:
    repository = FakeRepository()
    repository.values[(123, 456)] = 10
    service = FakeService(
        [page(stolen_vehicle(12), stolen_vehicle(11), stolen_vehicle(10))]
    )
    channel = FakeChannel()
    worker = StolenVehicleFeedWorker(
        SimpleNamespace(is_ready=lambda: True),  # type: ignore[arg-type]
        FakeProvider(FakeSnapshot()),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        service,  # type: ignore[arg-type]
        poll_seconds=60,
        enabled_guild_ids=(123,),
    )
    worker._resolve_channel = AsyncMock(return_value=channel)  # type: ignore[method-assign]

    await worker.process_once()

    assert repository.advances == [11, 12]
    assert [embed.title for embed in channel.embeds] == [
        "⚠️🚘 Vehículo denunciado como robado",
        "⚠️🚘 Vehículo denunciado como robado",
    ]


class FakeClient:
    def __init__(self, response: SarpStolenVehiclePage) -> None:
        self.response = response
        self.kwargs: dict[str, object] = {}

    async def list_stolen_vehicles(self, **kwargs: object) -> SarpStolenVehiclePage:
        self.kwargs = kwargs
        return self.response


async def test_service_uses_one_request_and_orders_newest_first() -> None:
    client = FakeClient(
        page(
            stolen_vehicle(1, active=False, created_minute=1),
            stolen_vehicle(2, active=False, created_minute=2),
            total=30,
        )
    )
    service = StolenVehicleService(client)  # type: ignore[arg-type]

    result = await service.list_recent(
        count=10,
        status=StolenVehicleStatusFilter.RESOLVED,
    )

    assert client.kwargs == {
        "page": 1,
        "limit": 10,
        "is_active": False,
    }
    assert [item.id for item in result.items] == [2, 1]
    assert result.available_count == 30


def test_alert_embed_has_detail_orange_color_and_safe_mentions() -> None:
    embed = build_stolen_vehicle_alert_embed(stolen_vehicle(20))
    serialized = str(embed.to_dict())

    assert embed.colour == discord.Color(0xE67E22)
    assert embed.title == "⚠️🚘 Vehículo denunciado como robado"
    assert "\\*\\*la estación\\*\\*" in (embed.description or "")
    assert "<@456>" in serialized
    assert next(field.value for field in embed.fields if field.name == "🔢 Matrícula") == (
        "`SV20`"
    )
    assert next(field.value for field in embed.fields if field.name == "🎨 Colores") == (
        "Negro/Blanco"
    )


def test_list_embed_is_compact_and_includes_legend_and_requester(persona: object) -> None:
    ui = persona.messages.interface.stolen_vehicles  # type: ignore[attr-defined]
    result = StolenVehicleQueryResult(
        items=(stolen_vehicle(20), stolen_vehicle(19, active=False)),
        requested_count=10,
        status=StolenVehicleStatusFilter.ALL,
        available_count=2,
    )

    embed = build_stolen_vehicle_list_embed(
        result,
        requester_id=449044161954447360,
        page=0,
        ui=ui,
    )

    assert embed.title == "🚘 Vehículos robados"
    assert "🔴 Activo · ⚪ Resuelto" in (embed.description or "")
    assert "`SV20` · Sultan" in (embed.description or "")
    assert "<@449044161954447360>" in (embed.description or "")
