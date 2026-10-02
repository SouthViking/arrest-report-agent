from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import discord
import pytest
from alembic import command
from alembic.config import Config

from arrest_report_agent.config import FtpRequestOptions, FtpStatusOptions
from arrest_report_agent.ftp_requests import (
    FtpInstructorLevel,
    FtpRequestRepository,
    FtpRequestStatus,
    FtpTrainingPhase,
    FtpTrainingRequest,
)
from arrest_report_agent.ftp_requests.discord import (
    FTP_TIMEZONE_OPTIONS,
    FtpActiveRequestsView,
    FtpRequestCog,
    FtpRequestControlsView,
    FtpRequestModal,
    FtpRequestPanelView,
    FtpRequestService,
    FtpTimezoneView,
    build_active_ftp_requests_embed,
    build_ftp_info_embed,
    build_ftp_request_embed,
    build_ftp_request_panel_embed,
    parse_ftp_schedule,
)
from arrest_report_agent.infrastructure.postgres import PostgresClient, PostgresClientConfig
from arrest_report_agent.infrastructure.postgres.schema import discord_guilds


def _member(user_id: int, role_ids: tuple[int, ...]) -> discord.Member:
    roles = {role_id: SimpleNamespace(id=role_id) for role_id in role_ids}
    guild = SimpleNamespace(id=1, get_role=roles.get, default_role=None)
    state = SimpleNamespace(store_user=lambda data: SimpleNamespace(id=int(data["id"])))
    return discord.Member(
        data={"user": {"id": str(user_id)}, "roles": [str(role_id) for role_id in role_ids]},
        guild=guild,
        state=state,
    )


def _service(persona) -> FtpRequestService:
    settings = SimpleNamespace(
        operational_staff_role_ids=(80,),
        ftp_status_options=FtpStatusOptions(
            faction_id=3,
            supervisory_role_ids=(10,),
            mfto_role_ids=(20,),
            fto_role_ids=(30,),
            ftt_role_ids=(40,),
        ),
        ftp_request_options=FtpRequestOptions(
            channel_id=50,
            additional_command_channel_ids=(51,),
            command_role_ids=(60,),
            executive_role_ids=(70,),
        ),
    )
    return FtpRequestService(
        SimpleNamespace(),
        SimpleNamespace(),
        settings,
        persona,
        enabled=True,
    )


def _request(
    status: FtpRequestStatus = FtpRequestStatus.PENDING,
    *,
    level: FtpInstructorLevel = FtpInstructorLevel.GENERAL,
    comments: str = "",
) -> FtpTrainingRequest:
    return FtpTrainingRequest(
        id=uuid4(),
        guild_id=1,
        channel_id=50,
        message_id=100,
        requester_user_id=200,
        reason=comments,
        training_phase=FtpTrainingPhase.PHASE_2,
        requested_instructor_level=level,
        scheduled_for=datetime.now(UTC) + timedelta(days=2),
        status=status,
        claimed_by_user_id=300 if status is FtpRequestStatus.CLAIMED else None,
        resolved_by_user_id=300 if status.terminal else None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        resolved_at=datetime.now(UTC) if status.terminal else None,
    )


def test_ftp_request_permissions_follow_the_role_matrix(persona) -> None:
    service = _service(persona)

    assert service.can_request(_member(1, (40,)))  # FTT
    assert not service.can_request(_member(1, (30,)))  # FTO
    assert service.can_request(_member(1, (60,)))  # Command
    assert service.can_request(_member(1, (70,)))  # Executive
    assert not service.can_request(_member(1, (999,)))

    general_request = _request()
    supervisor_request = _request(level=FtpInstructorLevel.SUPERVISOR)
    assert service.can_claim(_member(1, (30,)), general_request)  # FTO
    assert service.can_claim(_member(1, (20,)), general_request)  # MFTO
    assert service.can_claim(_member(1, (10,)), general_request)  # Supervisory
    assert service.can_claim(_member(1, (60,)), general_request)  # Command
    assert service.can_claim(_member(1, (70,)), general_request)  # Executive
    assert not service.can_claim(_member(1, (30,)), supervisor_request)
    assert not service.can_claim(_member(1, (20,)), supervisor_request)
    assert service.can_claim(_member(1, (10,)), supervisor_request)  # Supervisory
    assert service.can_claim(_member(1, (60,)), supervisor_request)  # Command
    assert service.can_claim(_member(1, (70,)), supervisor_request)  # Executive
    assert not service.can_claim(_member(1, (40,)), general_request)  # FTT

    request = _request()
    assert service.can_cancel(_member(200, (40,)), request)
    assert service.can_cancel(_member(1, (60,)), request)
    assert service.can_cancel(_member(1, (70,)), request)
    assert not service.can_cancel(_member(1, (30,)), request)
    assert service.is_command_channel(50)
    assert service.is_command_channel(51)
    assert not service.is_command_channel(52)
    assert service.command_channel_mentions() == "<#50> o <#51>"
    assert service.can_list_active(_member(1, (80,)))
    assert not service.can_list_active(_member(1, (40,)))


@pytest.mark.asyncio
async def test_ftp_request_modal_exposes_only_general_and_supervisor(persona) -> None:
    modal = FtpRequestModal(
        _service(persona),
        _member(1, (40,)),
        "America/Argentina/Buenos_Aires",
    )

    assert [(option.label, option.value) for option in modal.instructor_level.options] == [
        ("General", FtpInstructorLevel.GENERAL.value),
        ("Supervisor", FtpInstructorLevel.SUPERVISOR.value),
    ]
    assert modal.comments.required is False
    assert modal.comments.style is discord.InputTextStyle.long
    assert modal.comments.max_length == 500
    assert modal.children[3].label == "Hora · America/Argentina/Buenos_Aires"


def test_ftp_request_commands_are_root_commands() -> None:
    assert FtpRequestCog.request_training.name == "solicitar-ficha"
    assert FtpRequestCog.active_requests.name == "fichas-activas"
    assert FtpRequestCog.info_ftp.name == "info-ftp"
    assert FtpRequestCog.request_training.parent is None
    assert FtpRequestCog.active_requests.parent is None
    assert FtpRequestCog.info_ftp.parent is None


def test_ftp_request_notifies_the_selected_evaluator_group(persona) -> None:
    service = _service(persona)
    roles = {role_id: SimpleNamespace(id=role_id) for role_id in (10, 20, 30, 40, 60, 70)}

    guild = SimpleNamespace(get_role=roles.get)
    assert [role.id for role in service.notification_roles(guild, FtpInstructorLevel.GENERAL)] == [
        30,
        20,
        10,
    ]
    assert [role.id for role in service.notification_roles(guild, FtpInstructorLevel.SUPERVISOR)] == [
        10
    ]


@pytest.mark.asyncio
async def test_terminal_ftp_request_view_disables_both_actions() -> None:
    view = FtpRequestControlsView(disabled=True)

    assert len(view.children) == 2
    assert all(getattr(child, "disabled", False) for child in view.children)


@pytest.mark.asyncio
async def test_ftp_request_panel_is_persistent_and_opens_the_form() -> None:
    view = FtpRequestPanelView()

    assert view.timeout is None
    assert len(view.children) == 2
    assert view.children[0].custom_id == "ftp-request:v1:open-form"
    assert view.children[0].label == "Solicitar ficha"
    assert view.children[1].custom_id == "ftp-availability:v1:open-form"
    assert view.children[1].label == "Ofrecer disponibilidad"


@pytest.mark.asyncio
async def test_ftp_request_panel_opens_timezone_selector_for_authorized_member(persona) -> None:
    service = _service(persona)
    response = SimpleNamespace(send_modal=AsyncMock(), send_message=AsyncMock())
    interaction = SimpleNamespace(
        client=SimpleNamespace(
            ftp_request_registry=SimpleNamespace(get=AsyncMock(return_value=service))
        ),
        guild_id=1,
        channel_id=50,
        user=_member(1, (40,)),
        response=response,
    )

    await FtpRequestPanelView().children[0].callback(interaction)

    response.send_modal.assert_not_awaited()
    response.send_message.assert_awaited_once()
    assert response.send_message.await_args.kwargs["ephemeral"] is True
    assert isinstance(response.send_message.await_args.kwargs["view"], FtpTimezoneView)


@pytest.mark.asyncio
async def test_ftp_timezone_selector_opens_modal_with_selected_timezone(persona) -> None:
    member = _member(1, (40,))
    view = FtpTimezoneView(_service(persona), member)
    select = view.children[0]
    select._selected_values = ["America/Bogota"]  # noqa: SLF001
    select._interaction = SimpleNamespace(data={})  # noqa: SLF001
    response = SimpleNamespace(send_modal=AsyncMock(), send_message=AsyncMock())
    interaction = SimpleNamespace(user=member, response=response)

    await select.callback(interaction)

    response.send_modal.assert_awaited_once()
    modal = response.send_modal.await_args.args[0]
    assert isinstance(modal, FtpRequestModal)
    assert modal.timezone_key == "America/Bogota"
    response.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_ftp_timezone_selector_uses_fixed_iana_options(persona) -> None:
    view = FtpTimezoneView(_service(persona), _member(1, (40,)))
    select = view.children[0]

    assert [(str(option.emoji), option.label, option.value) for option in select.options] == [
        (emoji, timezone_key, timezone_key)
        for emoji, timezone_key in FTP_TIMEZONE_OPTIONS
    ]


@pytest.mark.asyncio
async def test_ftp_request_panel_is_published_persisted_and_pinned(
    persona,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SimpleNamespace(
        get_panel=AsyncMock(return_value=None),
        save_panel=AsyncMock(),
    )
    service = _service(persona)
    service.repository = repository
    message = SimpleNamespace(
        id=900,
        pinned=False,
        pin=AsyncMock(),
        delete=AsyncMock(),
    )
    channel = SimpleNamespace(id=50, send=AsyncMock(return_value=message))
    monkeypatch.setattr(service, "destination_channel", AsyncMock(return_value=channel))
    guild = SimpleNamespace(id=1)

    published = await service.ensure_request_panel(guild)

    assert published is message
    channel.send.assert_awaited_once()
    repository.save_panel.assert_awaited_once_with(
        guild_id=1,
        channel_id=50,
        message_id=900,
    )
    message.pin.assert_awaited_once_with(reason="Panel de solicitudes de fichas FTP")


def test_ftp_request_embed_is_neutral_and_disables_technical_ids() -> None:
    requester = SimpleNamespace(
        display_name="C. Harrington",
        display_avatar=SimpleNamespace(url="https://cdn.example/avatar.png"),
    )
    pending = build_ftp_request_embed(
        _request(comments="Coordinar punto de encuentro antes de comenzar."),
        requester=requester,
    )
    claimed = build_ftp_request_embed(_request(FtpRequestStatus.CLAIMED))

    assert pending.title == "🎓 Solicitud de ficha FTP"
    assert pending.author.name == "C. Harrington"
    assert pending.author.icon_url == "https://cdn.example/avatar.png"
    assert all(field.value != "100" for field in pending.fields)
    assert any(field.name == "Ficha" and field.value == "Fase 2" for field in pending.fields)
    assert any(
        field.name == "Tipo de evaluador" and field.value == "General"
        for field in pending.fields
    )
    assert any(field.name == "Fecha" and "<t:" in field.value for field in pending.fields)
    assert any(
        field.name == "Comentarios"
        and field.value == "Coordinar punto de encuentro antes de comenzar."
        for field in pending.fields
    )
    assert any(field.name == "Estado" and field.value == "Esperando instructor" for field in pending.fields)
    assert any(field.name == "Instructor asignado" and field.value == "<@300>" for field in claimed.fields)


@pytest.mark.asyncio
async def test_claim_replies_with_coordination_embed_and_mentions_both_participants(persona):
    service = _service(persona)
    pending = _request(comments="Practicar detenciones.")
    claimed = pending.model_copy(
        update={"status": FtpRequestStatus.CLAIMED, "claimed_by_user_id": 300}
    )
    service.resolve = AsyncMock(return_value=pending)
    service.repository = SimpleNamespace(claim=AsyncMock(return_value=(claimed, True)))
    service._edit_interaction_message = AsyncMock()
    interaction = SimpleNamespace(
        user=_member(300, (30,)),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        message=SimpleNamespace(reply=AsyncMock(), delete=AsyncMock()),
    )

    await service.claim(interaction)

    service._edit_interaction_message.assert_awaited_once_with(interaction, claimed)
    interaction.message.reply.assert_awaited_once()
    reply = interaction.message.reply.await_args.kwargs
    assert reply["content"] == "<@200> <@300>"
    assert reply["allowed_mentions"].to_dict() == {"users": [200, 300], "parse": []}
    embed = reply["embed"]
    assert embed.title == "🤝 Ficha coordinada"
    assert embed.color == discord.Color.blue()
    assert "**Fase:** Fase 2" in embed.description
    assert f"**Horario:** <t:{int(claimed.scheduled_for.timestamp())}:f>" in embed.description
    assert "**Trainee:** <@200>\n**Instructor:** <@300>" in embed.description
    assert "Bloque" not in embed.description
    assert embed.fields[0].value == claimed.reason
    interaction.followup.send.assert_not_awaited()
    interaction.message.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_claim_does_not_notify_twice_when_another_instructor_took_it(persona):
    service = _service(persona)
    service.resolve = AsyncMock(return_value=_request())
    service.repository = SimpleNamespace(
        claim=AsyncMock(return_value=(_request(FtpRequestStatus.CLAIMED), False))
    )
    interaction = SimpleNamespace(
        user=_member(300, (30,)),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        message=SimpleNamespace(reply=AsyncMock()),
    )
    await service.claim(interaction)
    interaction.message.reply.assert_not_awaited()
    assert interaction.followup.send.await_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_coordination_notification_failure_is_logged(persona, caplog):
    service = _service(persona)
    error = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing permissions")
    interaction = SimpleNamespace(message=SimpleNamespace(reply=AsyncMock(side_effect=error)))
    await service._notify_requester(interaction, _request(FtpRequestStatus.CLAIMED), _member(300, (30,)))
    assert "ftp_request_requester_notification_failed" in caplog.text


def test_active_ftp_requests_embed_is_compact_and_links_each_request() -> None:
    embed = build_active_ftp_requests_embed((_request(),), total=1, page=0)

    assert embed.title == "🎓 Solicitudes de fichas activas"
    assert embed.description == "**1 solicitud pendiente**"
    assert len(embed.fields) == 1
    assert embed.fields[0].name == "Fase 2 · 👨‍🏫 General"
    assert "<@200>" in embed.fields[0].value
    assert "<t:" in embed.fields[0].value
    assert "[Ver solicitud](https://discord.com/channels/1/50/100)" in embed.fields[0].value
    assert "Coordinar" not in str(embed.to_dict())

    empty = build_active_ftp_requests_embed((), total=0, page=0)
    assert empty.description == "No hay solicitudes pendientes en este momento."


@pytest.mark.asyncio
async def test_active_ftp_requests_view_queries_pending_requests(persona) -> None:
    service = _service(persona)
    service.repository = SimpleNamespace(
        list_pending=AsyncMock(return_value=((_request(),), 1)),
    )
    view = FtpActiveRequestsView(service, requester_id=80)

    embed = await view.render(1)

    service.repository.list_pending.assert_awaited_once_with(1, offset=0, limit=10)
    assert embed.description == "**1 solicitud pendiente**"
    assert view.previous.disabled is True
    assert view.next.disabled is True


def test_parse_ftp_schedule_uses_selected_timezone_and_accepts_valid_date() -> None:
    assert parse_ftp_schedule("26/09/2026", "20:00", "America/Santiago") == datetime(
        2026,
        9,
        26,
        23,
        0,
        tzinfo=UTC,
    )
    assert parse_ftp_schedule("26/09/2026", "20:00", "America/Bogota") == datetime(
        2026,
        9,
        27,
        1,
        0,
        tzinfo=UTC,
    )
    with pytest.raises(ValueError, match="Invalid"):
        parse_ftp_schedule("mañana", "después", "Europe/Madrid")
    with pytest.raises(ValueError, match="timezone"):
        parse_ftp_schedule("26/09/2026", "20:00", "Etc/GMT+3")


def test_ftp_info_embed_points_to_the_configured_channel(persona) -> None:
    embed = build_ftp_info_embed(_service(persona))

    assert embed.title == "🎓 Fichas del Field Training Program"
    assert any("<#50>" in field.value for field in embed.fields)
    assert any("<#51>" in field.value for field in embed.fields)
    assert any("/solicitar-ficha" in field.value for field in embed.fields)
    assert any("/fichas-activas" in field.value for field in embed.fields)
    assert any("mensaje fijado" in field.value for field in embed.fields)
    assert "superior" not in str(embed.to_dict()).lower()


def test_ftp_request_panel_explains_repeatable_scheduled_requests(persona) -> None:
    embed = build_ftp_request_panel_embed(_service(persona))

    assert embed.title == "🎓 Fichas FTP"
    assert "Pulsa **Solicitar ficha**" in (embed.description or "")
    assert "**Ofrecer disponibilidad**" in (embed.description or "")
    assert "varias solicitudes" in (embed.description or "")


def test_ftp_request_options_reject_overlap_between_management_roles() -> None:
    with pytest.raises(ValueError, match="unique"):
        FtpRequestOptions(
            channel_id=50,
            command_role_ids=(60,),
            executive_role_ids=(60,),
        )
    with pytest.raises(ValueError, match="channel IDs must be unique"):
        FtpRequestOptions(
            channel_id=50,
            additional_command_channel_ids=(50,),
            command_role_ids=(60,),
            executive_role_ids=(70,),
        )


@pytest.mark.asyncio
async def test_ftp_request_repository_allows_multiple_scheduled_requests_and_transitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("requires TEST_DATABASE_URL with an isolated PostgreSQL database")
    monkeypatch.setenv("DATABASE_URL", database_url)
    await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    guild_id = 975432101234567800
    requester_id = 975432101234567801
    try:
        await client.execute(discord_guilds.delete().where(discord_guilds.c.discord_id == guild_id))
        await client.execute(discord_guilds.insert().values(discord_id=guild_id, name="FTP requests QA"))
        repository = FtpRequestRepository(client)

        async def reserve():
            return await repository.create_publishing(
                guild_id=guild_id,
                channel_id=975432101234567802,
                requester_user_id=requester_id,
                training_phase=FtpTrainingPhase.PHASE_2,
                requested_instructor_level=FtpInstructorLevel.SUPERVISOR,
                scheduled_for=datetime.now(UTC) + timedelta(days=2),
                reason="Preferencia por horario nocturno.",
            )

        outcomes = await asyncio.gather(reserve(), reserve(), return_exceptions=True)
        created = [outcome for outcome in outcomes if isinstance(outcome, FtpTrainingRequest)]
        assert len(created) == 2
        assert all(not isinstance(outcome, Exception) for outcome in outcomes)

        request = await repository.publish(created[0].id, 975432101234567803)
        assert request.status is FtpRequestStatus.PENDING
        assert request.training_phase is FtpTrainingPhase.PHASE_2
        assert request.requested_instructor_level is FtpInstructorLevel.SUPERVISOR
        assert request.scheduled_for is not None
        assert request.reason == "Preferencia por horario nocturno."
        restored = await FtpRequestRepository(client).get_by_message(
            guild_id,
            request.channel_id,
            request.message_id or 0,
        )
        assert restored is not None and restored.id == request.id

        second = await repository.publish(created[1].id, 975432101234567806)
        pending, total = await repository.list_pending(guild_id, offset=0, limit=10)
        assert total == 2
        assert {item.id for item in pending} == {request.id, second.id}

        panel = await repository.save_panel(
            guild_id=guild_id,
            channel_id=975432101234567802,
            message_id=975432101234567899,
        )
        assert panel.guild_id == guild_id
        assert await repository.get_panel(guild_id) == panel

        claims = await asyncio.gather(
            repository.claim(request.id, actor_user_id=975432101234567804),
            repository.claim(request.id, actor_user_id=975432101234567805),
        )
        assert sum(changed for _, changed in claims) == 1
        claimed = next(item for item, changed in claims if changed)
        assert claimed.status is FtpRequestStatus.CLAIMED

        pending, total = await repository.list_pending(guild_id, offset=0, limit=10)
        assert total == 1
        assert [item.id for item in pending] == [second.id]

        another = await reserve()
        assert another.id != request.id
    finally:
        await client.execute(discord_guilds.delete().where(discord_guilds.c.discord_id == guild_id))
        await client.close()
