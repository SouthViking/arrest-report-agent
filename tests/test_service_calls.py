from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from arrest_report_agent.config import AccessPolicyKey, AccessPolicyMode
from arrest_report_agent.infrastructure.sarp import SarpEmergencyCall, SarpEmergencyCallPage
from arrest_report_agent.service_calls import ServiceCallFeedWorker, build_service_call_embed
from arrest_report_agent.service_calls.repository import ServiceCallCursor


def emergency_call(call_id: int, *, situation: str | None = None) -> SarpEmergencyCall:
    return SarpEmergencyCall(
        id=call_id,
        character_id=10,
        character_name="Andrew_McFly",
        phone_number=0,
        trace="Mulholland Drive",
        service="LSPD",
        location="Mulholland Drive",
        situation=situation or f"Situación {call_id}",
        created_at=datetime(2026, 8, 17, 22, call_id % 60, tzinfo=UTC),
    )


class FakeSnapshot:
    guild_id = 123

    def __init__(self, *, enabled: bool = True, channel_ids: tuple[int, ...] = (456,)) -> None:
        self.features = {"service_calls": SimpleNamespace(enabled=enabled)}
        mode = AccessPolicyMode.RESTRICTED if channel_ids else AccessPolicyMode.DISABLED
        self.policies = {
            AccessPolicyKey.SERVICE_CALL_CHANNELS: SimpleNamespace(
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

    async def get(self, guild_id: int, channel_id: int) -> ServiceCallCursor | None:
        value = self.values.get((guild_id, channel_id))
        return (
            ServiceCallCursor(guild_id, channel_id, value)
            if value is not None
            else None
        )

    async def initialize(self, guild_id: int, channel_id: int, call_id: int) -> bool:
        key = (guild_id, channel_id)
        if key in self.values:
            return False
        self.values[key] = call_id
        return True

    async def advance(self, guild_id: int, channel_id: int, call_id: int) -> None:
        self.values[(guild_id, channel_id)] = call_id
        self.advances.append(call_id)


class FakeClient:
    def __init__(self, pages: list[SarpEmergencyCallPage]) -> None:
        self.pages = pages
        self.calls = 0

    async def list_emergency_calls(self, *, page: int, limit: int) -> SarpEmergencyCallPage:
        assert limit == 100
        self.calls += 1
        return self.pages.pop(0)


class FakeChannel:
    def __init__(self) -> None:
        self.embeds: list[discord.Embed] = []

    async def send(self, *, embed: discord.Embed, allowed_mentions: object) -> object:
        assert isinstance(allowed_mentions, discord.AllowedMentions)
        self.embeds.append(embed)
        return SimpleNamespace(id=9000 + len(self.embeds))


def page(*calls: SarpEmergencyCall) -> SarpEmergencyCallPage:
    return SarpEmergencyCallPage(
        items=list(calls),
        total=len(calls),
        page=1,
        limit=100,
        pages=1,
    )


async def test_feed_does_not_call_sarp_without_configured_channels() -> None:
    client = FakeClient([page(emergency_call(10))])
    worker = ServiceCallFeedWorker(
        SimpleNamespace(is_ready=lambda: True),  # type: ignore[arg-type]
        FakeProvider(FakeSnapshot(enabled=False, channel_ids=())),  # type: ignore[arg-type]
        FakeRepository(),  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        poll_seconds=30,
    )

    worker.start()
    await worker.process_once()

    assert worker.running is False
    assert client.calls == 0


async def test_first_sync_initializes_cursor_without_replaying_history() -> None:
    repository = FakeRepository()
    client = FakeClient([page(emergency_call(12), emergency_call(11))])
    channel = FakeChannel()
    worker = ServiceCallFeedWorker(
        SimpleNamespace(is_ready=lambda: True),  # type: ignore[arg-type]
        FakeProvider(FakeSnapshot()),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        poll_seconds=30,
        enabled_guild_ids=(123,),
    )
    worker._resolve_channel = AsyncMock(return_value=channel)  # type: ignore[method-assign]

    await worker.process_once()

    assert repository.values[(123, 456)] == 12
    assert repository.advances == []
    assert channel.embeds == []


async def test_feed_delivers_only_new_calls_oldest_first_and_advances_cursor() -> None:
    repository = FakeRepository()
    repository.values[(123, 456)] = 10
    client = FakeClient([page(emergency_call(12), emergency_call(11), emergency_call(10))])
    channel = FakeChannel()
    worker = ServiceCallFeedWorker(
        SimpleNamespace(is_ready=lambda: True),  # type: ignore[arg-type]
        FakeProvider(FakeSnapshot()),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        poll_seconds=30,
        enabled_guild_ids=(123,),
    )
    worker._resolve_channel = AsyncMock(return_value=channel)  # type: ignore[method-assign]

    await worker.process_once()

    assert repository.advances == [11, 12]
    assert [embed.footer.text for embed in channel.embeds] == [
        "Llamada #00011 · Fecha informada en UTC",
        "Llamada #00012 · Fecha informada en UTC",
    ]


def test_service_call_embed_is_safe_and_omits_empty_phone() -> None:
    embed = build_service_call_embed(
        emergency_call(56221, situation="Avisan a <@123> por **un robo**")
    )

    assert embed.title == "🚨 Llamada de servicio"
    assert "<@123>" in (embed.description or "")
    assert "\\*\\*un robo\\*\\*" in (embed.description or "")
    assert next(field.value for field in embed.fields if field.name == "📞 Teléfono") == (
        "No informado"
    )
    assert embed.timestamp == datetime(2026, 8, 17, 22, 1, tzinfo=UTC)
