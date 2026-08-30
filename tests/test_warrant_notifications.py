from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from arrest_report_agent.config import WarrantNotificationOptions
from arrest_report_agent.infrastructure.sarp import (
    SarpNotFoundError,
    SarpRateLimit,
    SarpWarrantDetail,
)
from arrest_report_agent.warrant_notifications import (
    WarrantDetail,
    WarrantLookupService,
    WarrantNotificationView,
    WarrantType,
    build_warrant_detail_embed,
    build_warrant_notification_embed,
)
from arrest_report_agent.warrant_notifications import discord as warrant_discord


def _payload() -> SarpWarrantDetail:
    return SarpWarrantDetail.model_validate(
        {
            "id": 1324,
            "suspect_id": 8225,
            "suspect_name": "Louie_Lazzara",
            "description": "Fundamento de la orden. " * 100,
            "type": ["arrest"],
            "risk": "high",
            "status": 0,
            "filed_by": 2044,
            "filed_name": "Dennis_Irvan",
            "signed_at": None,
            "signed_by": 0,
            "signed_name": "Firstname_Lastname",
            "closed_at": "2026-08-11 11:25:56",
            "closed_by": 2044,
            "closed_name": "Police Detective III Dennis Irvan",
            "created_at": "2026-08-10 11:49:15",
            "updated_at": "2026-08-11 11:25:56",
            "charges": [
                {
                    "charge_id": 156,
                    "code": "CP-189/664",
                    "charge": "Intento de homicidio",
                    "type": "GRAVE",
                    "hours": 3,
                    "fine": 7200,
                }
            ],
        }
    )


class FakeWarrantClient:
    def __init__(self, result: SarpWarrantDetail | Exception) -> None:
        self.result = result
        self.calls: list[int] = []

    async def get_warrant(self, warrant_id: int) -> SarpWarrantDetail:
        self.calls.append(warrant_id)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.mark.asyncio
async def test_lookup_maps_complete_warrant_and_removes_placeholders() -> None:
    client = FakeWarrantClient(_payload())
    result = await WarrantLookupService(client).get(1324)

    assert client.calls == [1324]
    assert result.error is None
    assert result.detail is not None
    assert result.detail.suspect_name == "Louie Lazzara"
    assert result.detail.filed_name == "Dennis Irvan"
    assert result.detail.signed_name is None
    assert result.detail.closed_name == "Police Detective III Dennis Irvan"
    assert result.detail.charges[0].code == "CP-189/664"


@pytest.mark.asyncio
async def test_lookup_returns_not_found_without_raising() -> None:
    error = SarpNotFoundError(
        status_code=404,
        message="Not found",
        source="police-data",
        request_id=None,
        rate_limit=SarpRateLimit(),
    )
    result = await WarrantLookupService(FakeWarrantClient(error)).get(9999)

    assert result.detail is None
    assert result.error == "not_found"


def test_notification_embed_is_compact_and_does_not_contain_live_detail() -> None:
    member = SimpleNamespace(
        display_name="Charles Harrington",
        display_avatar=SimpleNamespace(url="https://cdn.example/avatar.png"),
    )
    embed = build_warrant_notification_embed(
        member,  # type: ignore[arg-type]
        report_url="https://sheriff.sarp.es/viewtopic.php?t=123",
        warrant_id=1324,
        warrant_type=WarrantType.BOTH,
    )

    payload = str(embed.to_dict())
    assert embed.title == "⚖️ Nueva orden de arresto y allanamiento"
    assert embed.author.name == "Charles Harrington"
    assert "N.º 1324" in payload
    assert "Abrir reporte de incidente" in payload
    assert "Louie" not in payload
    assert "Cargos" not in payload


@pytest.mark.asyncio
async def test_publishing_notification_does_not_query_sarp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeWarrantClient(_payload())
    lookup = WarrantLookupService(client)
    options = WarrantNotificationOptions(
        channel_id=50,
        command_role_ids=(10,),
        executive_role_ids=(20,),
    )
    settings = SimpleNamespace(
        warrant_notification_options=options,
        operational_staff_role_ids=(30,),
    )
    provider = SimpleNamespace(
        cached=AsyncMock(return_value=SimpleNamespace(settings=settings))
    )
    channel = SimpleNamespace(id=50, send=AsyncMock(return_value=SimpleNamespace(id=99)))
    roles = (
        SimpleNamespace(id=20, mention="<@&20>"),
        SimpleNamespace(id=10, mention="<@&10>"),
    )
    monkeypatch.setattr(warrant_discord, "_destination_channel", AsyncMock(return_value=channel))
    monkeypatch.setattr(warrant_discord, "_notification_roles", lambda _guild, _options: roles)
    cog = warrant_discord.WarrantNotificationCog(provider, lookup)  # type: ignore[arg-type]
    member = SimpleNamespace(
        id=1,
        guild=SimpleNamespace(id=2),
        roles=(SimpleNamespace(id=30),),
        display_name="Charles Harrington",
        display_avatar=SimpleNamespace(url="https://cdn.example/avatar.png"),
    )
    interaction = SimpleNamespace(
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )

    await cog.publish(
        interaction,  # type: ignore[arg-type]
        member=member,  # type: ignore[arg-type]
        report_url="https://sheriff.sarp.es/viewtopic.php?t=123",
        warrant_id=1324,
        warrant_type=WarrantType.ARREST,
    )

    assert client.calls == []
    channel.send.assert_awaited_once()
    interaction.followup.send.assert_awaited_once()
    assert interaction.followup.send.await_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_detail_button_fetches_once_and_adds_public_detail_embed() -> None:
    client = FakeWarrantClient(_payload())
    lookup = WarrantLookupService(client)
    original = build_warrant_notification_embed(
        SimpleNamespace(
            display_name="Charles Harrington",
            display_avatar=SimpleNamespace(url="https://cdn.example/avatar.png"),
        ),  # type: ignore[arg-type]
        report_url="https://sheriff.sarp.es/viewtopic.php?t=123",
        warrant_id=1324,
        warrant_type=WarrantType.ARREST,
    )
    message = SimpleNamespace(id=99, embeds=[original], edit=AsyncMock())
    interaction = SimpleNamespace(
        message=message,
        guild_id=2,
        channel_id=50,
        user=SimpleNamespace(id=1),
        response=SimpleNamespace(defer=AsyncMock(), edit_message=AsyncMock()),
    )
    view = WarrantNotificationView(lookup)
    button = view.children[0]

    await button.callback(interaction)  # type: ignore[arg-type]

    assert client.calls == [1324]
    assert message.edit.await_count == 2
    final_call = message.edit.await_args_list[-1]
    assert len(final_call.kwargs["embeds"]) == 2
    assert final_call.kwargs["embeds"][1].title == "📋 Detalle de la orden N.º 1324"


@pytest.mark.asyncio
async def test_detail_button_renders_public_not_found_alert() -> None:
    error = SarpNotFoundError(
        status_code=404,
        message="Not found",
        source="police-data",
        request_id=None,
        rate_limit=SarpRateLimit(),
    )
    lookup = WarrantLookupService(FakeWarrantClient(error))
    original = build_warrant_notification_embed(
        SimpleNamespace(
            display_name="Charles Harrington",
            display_avatar=SimpleNamespace(url="https://cdn.example/avatar.png"),
        ),  # type: ignore[arg-type]
        report_url="https://sheriff.sarp.es/viewtopic.php?t=123",
        warrant_id=9999,
        warrant_type=WarrantType.SEARCH,
    )
    message = SimpleNamespace(id=99, embeds=[original], edit=AsyncMock())
    interaction = SimpleNamespace(
        message=message,
        guild_id=2,
        channel_id=50,
        user=SimpleNamespace(id=1),
        response=SimpleNamespace(defer=AsyncMock(), edit_message=AsyncMock()),
    )
    view = WarrantNotificationView(lookup)

    await view.children[0].callback(interaction)  # type: ignore[arg-type]

    final_call = message.edit.await_args_list[-1]
    assert final_call.kwargs["embeds"][1].title == "⚠️ Orden no encontrada"
    assert getattr(final_call.kwargs["view"].children[0], "label", None) == (
        "Reintentar consulta"
    )


def test_detail_embed_chunks_long_narrative_and_omits_placeholders() -> None:
    payload = _payload()
    detail = WarrantDetail(
        warrant_id=payload.id,
        suspect_name="Louie Lazzara",
        description=payload.description,
        types=payload.types,
        risk=payload.risk,
        status=payload.status,
        filed_name="Dennis Irvan",
        signed_at=None,
        signed_name=None,
        closed_at=payload.closed_at,
        closed_name="Police Detective III Dennis Irvan",
        created_at=payload.created_at,
        updated_at=payload.updated_at,
        charges=(),
    )
    embed = build_warrant_detail_embed(detail)

    fields = list(embed.fields)
    narrative_fields = [field for field in fields if field.name.startswith("📝 Fundamento")]
    assert embed.title == "📋 Detalle de la orden N.º 1324"
    assert len(narrative_fields) >= 2
    assert all(len(str(field.value)) <= 1024 for field in fields)
    assert "Firstname" not in str(embed.to_dict())
    assert any("<t:" in str(field.value) for field in fields if field.name == "👮 Tramitación")


@pytest.mark.asyncio
async def test_persistent_view_switches_labels_without_storing_warrant_data() -> None:
    lookup = WarrantLookupService(FakeWarrantClient(_payload()))
    collapsed = WarrantNotificationView(lookup)
    expanded = WarrantNotificationView(
        lookup,
        mode="expanded",
    )
    retry = WarrantNotificationView(lookup, mode="retry")

    assert collapsed.timeout is None
    assert getattr(collapsed.children[0], "label", None) == "Ver detalle"
    assert getattr(expanded.children[0], "label", None) == "Ocultar detalle"
    assert getattr(retry.children[0], "label", None) == "Reintentar consulta"


def test_warrant_notification_options_keep_destination_and_mentions_configurable() -> None:
    options = WarrantNotificationOptions(
        channel_id=1261076248541532240,
        command_role_ids=(10,),
        executive_role_ids=(20,),
    )

    assert options.notification_role_ids == (20, 10)
    with pytest.raises(ValueError, match="unique"):
        WarrantNotificationOptions(
            channel_id=1261076248541532240,
            command_role_ids=(10,),
            executive_role_ids=(10,),
        )
