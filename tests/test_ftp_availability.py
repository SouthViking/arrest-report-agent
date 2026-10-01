from __future__ import annotations

import asyncio
import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import uuid4
from zoneinfo import ZoneInfo

import discord
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from discord.webhook.async_ import async_context
from test_ftp_requests import _member, _service

from arrest_report_agent.ftp_requests.availability_discord import (
    AvailabilityCog,
    AvailabilityControlsView,
    AvailabilityModal,
    AvailabilityService,
    AvailabilitySetupView,
    ReservationModal,
    build_booking_embed,
    build_offer_embed,
    can_close,
    can_offer,
    can_reserve,
    clear_ephemeral_confirmation,
)
from arrest_report_agent.ftp_requests.availability_models import (
    AvailabilityOffer,
    AvailabilitySlot,
    block_times,
)
from arrest_report_agent.ftp_requests.availability_repository import AvailabilityRepository
from arrest_report_agent.ftp_requests.discord import (
    FtpRequestPanelView,
    build_ftp_info_embed,
    parse_ftp_schedule,
)
from arrest_report_agent.ftp_requests.models import FtpTrainingPhase
from arrest_report_agent.infrastructure.postgres import PostgresClient, PostgresClientConfig
from arrest_report_agent.infrastructure.postgres.schema import discord_guilds
from arrest_report_agent.infrastructure.postgres.schema import ftp_availability_offers as offers
from arrest_report_agent.infrastructure.postgres.schema import ftp_training_requests as requests


def _offer(**updates):
    now = datetime.now(UTC)
    offer_id = uuid4()
    return AvailabilityOffer(
        id=offer_id,
        guild_id=1,
        channel_id=50,
        message_id=100,
        interaction_id=99,
        instructor_id=300,
        comments="Disponibilidad de prueba",
        created_at=now,
        updated_at=now,
        revision=0,
        status="open",
        slots=tuple(
            AvailabilitySlot(
                id=uuid4(),
                offer_id=offer_id,
                position=index,
                starts_at=start,
                ends_at=end,
                status="free",
            )
            for index, (start, end) in enumerate(block_times(now + timedelta(hours=1), 60, 3), 1)
        ),
    ).model_copy(update=updates)


def _interaction(user=None, channel_id=50):
    return SimpleNamespace(
        id=99,
        guild_id=1,
        channel_id=channel_id,
        user=user or _member(200, (40,)),
        message=SimpleNamespace(id=100),
        response=SimpleNamespace(
            send_modal=AsyncMock(),
            send_message=AsyncMock(),
            edit_message=AsyncMock(),
            defer=AsyncMock(),
            is_done=Mock(return_value=False),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
        original_response=AsyncMock(
            return_value=SimpleNamespace(id=101, flags=discord.MessageFlags(ephemeral=True))
        ),
        delete_original_response=AsyncMock(),
    )


@contextmanager
def _pycord_response(interaction, kind):
    """Exercise Pycord's actual defer implementation, mocking only the HTTP adapter."""
    interaction.type = kind
    interaction.token = "test-interaction-token"
    interaction._session = None
    interaction._state = SimpleNamespace(http=SimpleNamespace(proxy=None, proxy_auth=None))
    interaction.response = discord.InteractionResponse(interaction)

    async def respond(*_args, **kwargs):
        ephemeral = bool((kwargs.get("data") or {}).get("flags", 0) & 64)
        if kwargs["type"] == discord.InteractionResponseType.deferred_message_update.value:
            interaction.original_response.return_value = interaction.message
        else:
            interaction.original_response.return_value = SimpleNamespace(
                id=101,
                flags=discord.MessageFlags(ephemeral=ephemeral),
            )
        return {"interaction": {"response_message_ephemeral": ephemeral}}

    adapter = SimpleNamespace(create_interaction_response=AsyncMock(side_effect=respond))
    token = async_context.set(adapter)
    try:
        yield adapter
    finally:
        async_context.reset(token)


def _availability(persona):
    existing = _service(persona)
    registry = SimpleNamespace(get=AsyncMock(return_value=existing))
    repository = SimpleNamespace(
        **{
            method: AsyncMock()
            for method in (
                "get",
                "create",
                "by_message",
                "publish",
                "reserve",
                "close",
                "expire",
                "due",
                "notified",
                "acknowledge",
                "fail_publication",
            )
        }
    )
    bot = SimpleNamespace(user=SimpleNamespace(id=999), get_guild=Mock())
    return AvailabilityService(bot, registry, repository), existing


@pytest.mark.parametrize("role", [10, 20, 30, 60, 70])
def test_only_instructor_roles_offer_and_only_ftt_reserves(persona, role):
    service = _service(persona)
    assert can_offer(service, _member(200, (role,)))
    assert not can_reserve(service, _member(200, (role,)))
    assert can_reserve(service, _member(200, (40,)))
    assert not can_offer(service, _member(200, (40,)))
    assert not can_offer(service, _member(200, (80,)))  # Operational staff alone is insufficient.
    assert not can_reserve(service, _member(200, (80,)))


def test_close_allowed_to_author_or_management_only(persona):
    service, offer = _service(persona), _offer()
    assert can_close(service, _member(300, (30,)), offer)
    assert can_close(service, _member(400, (60,)), offer)
    assert can_close(service, _member(400, (70,)), offer)
    assert not can_close(service, _member(400, (10,)), offer)
    assert not can_close(service, _member(400, (40,)), offer)


def test_blocks_convert_to_utc_and_cross_midnight_without_ambiguity():
    start = parse_ftp_schedule("03/10/2026", "22:00", "America/Santiago")
    blocks = block_times(start, 60, 4)
    assert start.tzinfo is UTC
    assert blocks[0][0] == datetime(2026, 10, 4, 1, tzinfo=UTC)
    assert blocks[-1][1].astimezone(ZoneInfo("America/Santiago")) == datetime(
        2026,
        10,
        4,
        2,
        tzinfo=ZoneInfo("America/Santiago"),
    )
    assert all(first[1] == second[0] for first, second in zip(blocks, blocks[1:], strict=False))
    for minutes, count in [(20, 2), (60, 0), (60, 6)]:
        with pytest.raises(ValueError):
            block_times(start, minutes, count)
    with pytest.raises(ValueError):
        block_times(datetime(2026, 10, 3), 60, 1)


@pytest.mark.asyncio
async def test_forms_fit_discord_limits_and_persistent_controls(persona):
    service, _ = _availability(persona)
    scheduled = AvailabilityModal(service, 200, "America/Santiago")
    immediate = AvailabilityModal(service, 200, None)
    assert len(scheduled.children) == 5
    assert len(immediate.children) == 3
    assert scheduled.comments.required is False
    assert [option.value for option in scheduled.duration.options] == ["30", "45", "60", "90"]
    assert [option.value for option in scheduled.count.options] == ["1", "2", "3", "4", "5"]
    offer = _offer()
    reservation = ReservationModal(service, 200, offer, offer.slots[0])
    assert len(reservation.children) == 2
    assert [option.value for option in reservation.phase.options] == ["phase_1", "phase_2", "phase_3"]
    assert reservation.comments.required is False
    view = AvailabilityControlsView(offer)
    assert view.is_persistent()
    assert AvailabilityControlsView().is_persistent()  # dispatcher registered at startup
    select = next(child for child in view.children if isinstance(child, discord.ui.Select))
    assert [option.label for option in select.options] == ["Bloque 1", "Bloque 2", "Bloque 3"]
    assert all("<t:" not in option.label for option in select.options)
    assert AvailabilityCog.offer_training.name == "ofrecer-ficha"
    assert AvailabilityCog.offer_training.parent is None
    assert len(scheduled.to_dict()["components"]) == 5


@pytest.mark.asyncio
async def test_expired_or_reserved_slots_removed_from_selector():
    offer = _offer()
    first = offer.slots[0].model_copy(
        update={"status": "reserved", "trainee_id": 200, "phase": FtpTrainingPhase.PHASE_1}
    )
    third = offer.slots[2].model_copy(update={"ends_at": datetime.now(UTC) - timedelta(seconds=1)})
    offer = offer.model_copy(update={"slots": (first, offer.slots[1], third)})
    select = next(
        child for child in AvailabilityControlsView(offer).children if isinstance(child, discord.ui.Select)
    )
    assert [option.label for option in select.options] == ["Bloque 2"]
    ended = offer.model_copy(update={"status": "closed"})
    assert all(child.disabled for child in AvailabilityControlsView(ended).children)


def test_render_uses_native_timestamps_and_author_and_no_unintended_mentions():
    offer = _offer(comments="Aviso @everyone **hola**")
    member = SimpleNamespace(
        display_name="Instructor", display_avatar=SimpleNamespace(url="https://x.test/a.png")
    )
    embed = build_offer_embed(offer, member)
    assert embed.author.name == "Instructor"
    assert embed.author.icon_url == "https://x.test/a.png"
    assert f"<t:{int(offer.slots[0].starts_at.timestamp())}:f>" in embed.fields[0].value
    assert "@everyone" not in embed.fields[-1].value
    booked = offer.slots[0].model_copy(
        update={
            "status": "reserved",
            "trainee_id": 200,
            "phase": FtpTrainingPhase.PHASE_2,
            "comments": "@here",
        }
    )
    offer = offer.model_copy(update={"slots": (booked,)})
    assert "Sin bloques disponibles" in build_offer_embed(offer).description
    assert "<@200> · Fase 2" in build_offer_embed(offer).fields[0].value
    result = build_booking_embed(offer, booked)
    assert "<@200>" in result.description and "<@300>" in result.description
    assert "Fase 2" in result.description


@pytest.mark.asyncio
async def test_selector_only_opens_modal_and_rechecks_permissions(persona):
    service, _ = _availability(persona)
    offer = _offer()
    service.repository.by_message.return_value = offer
    interaction = _interaction()
    await service.select(interaction, str(offer.slots[0].id))
    interaction.response.send_modal.assert_awaited_once()
    assert isinstance(interaction.response.send_modal.await_args.args[0], ReservationModal)
    service.repository.reserve.assert_not_awaited()
    staff = _interaction(_member(200, (60,)))
    await service.select(staff, str(offer.slots[0].id))
    staff.response.send_modal.assert_not_awaited()
    assert staff.response.send_message.await_args.kwargs["ephemeral"] is True
    # A role removed between opening a modal and submitting it cannot be used to book.
    await service.reserve(staff, offer.id, offer.slots[0].id, FtpTrainingPhase.PHASE_1, "")
    service.repository.reserve.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_setup_requires_invoker_and_configured_entry_channel(persona):
    service, _ = _availability(persona)
    view = AvailabilitySetupView(service, 200)
    assert not await view.interaction_check(_interaction(_member(300, (30,))))
    invalid = _interaction(_member(200, (30,)), channel_id=52)
    await service.open_form(invalid)
    assert "<#50> o <#51>" in invalid.response.send_message.await_args.args[0]
    for channel_id in [50, 51]:
        interaction = _interaction(_member(200, (30,)), channel_id=channel_id)
        await service.open_form(interaction)
        assert isinstance(interaction.response.send_message.await_args.kwargs["view"], AvailabilitySetupView)
    assert "ofrecer-ficha" in str(build_ftp_info_embed(_service(persona)).to_dict())
    panel = FtpRequestPanelView()
    interaction.client = SimpleNamespace(ftp_availability_service=service)
    service.open_form = AsyncMock()
    await panel.children[1].callback(interaction)
    service.open_form.assert_awaited_once_with(interaction)


@pytest.mark.asyncio
async def test_modal_now_uses_submit_time_and_owner_is_checked(persona):
    service, _ = _availability(persona)
    service.create = AsyncMock()
    modal = AvailabilityModal(service, 200, None)
    modal.duration._selected_values = ["60"]
    modal.count._selected_values = ["2"]
    modal.duration._interaction = SimpleNamespace(data={})
    modal.count._interaction = SimpleNamespace(data={})
    wrong = _interaction(_member(201, (30,)))
    await modal.callback(wrong)
    service.create.assert_not_awaited()
    before = datetime.now(UTC)
    await modal.callback(_interaction(_member(200, (30,))))
    args = service.create.await_args.kwargs
    assert before <= args["start"] <= datetime.now(UTC)
    assert args["minutes"] == 60 and args["count"] == 2


def _discord_sync(service, offer, *, error=None):
    message = SimpleNamespace(
        id=100,
        author=SimpleNamespace(id=999),
        embeds=[],
        edit=AsyncMock(side_effect=error),
        reply=AsyncMock(),
        delete=AsyncMock(),
        flags=discord.MessageFlags(),
    )
    channel = MagicMock(spec=discord.TextChannel)
    channel.fetch_message = AsyncMock(return_value=message)

    async def history(**_):
        for item in []:
            yield item

    channel.history = history
    guild = SimpleNamespace(get_channel=Mock(return_value=channel), get_member=Mock(return_value=None))
    service.bot.get_guild.return_value = guild
    service.repository.get.return_value = offer
    return message, channel, guild


@pytest.mark.asyncio
async def test_sync_edits_same_card_without_role_ping_notifies_both_and_acknowledges(persona):
    service, _ = _availability(persona)
    offer = _offer()
    booked = offer.slots[0].model_copy(
        update={
            "status": "reserved",
            "trainee_id": 200,
            "phase": FtpTrainingPhase.PHASE_1,
            "notification_due": True,
        }
    )
    offer = offer.model_copy(update={"slots": (booked, *offer.slots[1:])})
    message, _, _ = _discord_sync(service, offer)
    await service.sync(offer)
    assert message.edit.await_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    args = message.reply.await_args.kwargs
    assert args["content"] == "<@200> <@300>"
    assert args["allowed_mentions"].to_dict() == {"users": [200, 300], "parse": []}
    assert args["nonce"] == booked.id.hex[:25]
    service.repository.notified.assert_awaited_once_with(booked.id)
    service.repository.acknowledge.assert_awaited_once_with(offer)


@pytest.mark.asyncio
async def test_failed_discord_edit_is_retried_without_losing_booking(persona):
    service, _ = _availability(persona)
    offer = _offer()
    message, _, _ = _discord_sync(service, offer, error=RuntimeError("Discord unavailable"))
    await service.sync(offer)
    message.reply.assert_not_awaited()
    service.repository.reserve.assert_not_awaited()
    assert service.repository.acknowledge.await_args.args[1] > datetime.now(UTC)
    message.edit.side_effect = None
    await service.sync(offer)
    assert service.repository.acknowledge.await_args.args == (offer,)


@pytest.mark.asyncio
async def test_publication_mentions_only_trainees_and_removes_private_confirmation(persona):
    service, existing = _availability(persona)
    member = _member(300, (30,))
    member.guild.get_role = {30: SimpleNamespace(id=30), 40: SimpleNamespace(id=40, mention="<@&40>")}.get
    member._user.name = "Instructor"
    member._user.global_name = None
    member._user.avatar = SimpleNamespace(url="https://x.test/avatar.png")
    interaction = _interaction(member, channel_id=51)
    draft = _offer(status="publishing", message_id=None)
    published = draft.model_copy(update={"status": "open", "message_id": 100})
    service.repository.create.return_value = draft
    service.repository.publish.return_value = published
    message = SimpleNamespace(id=100, delete=AsyncMock())
    channel = SimpleNamespace(id=50, send=AsyncMock(return_value=message))
    existing.destination_channel = AsyncMock(return_value=channel)
    service.sync = AsyncMock()
    with _pycord_response(interaction, discord.InteractionType.modal_submit) as adapter:
        await service.create(interaction, start=draft.slots[0].starts_at, minutes=60, count=3, comments="")
    ack = adapter.create_interaction_response.await_args.kwargs
    assert ack["type"] == discord.InteractionResponseType.deferred_channel_message.value
    assert ack["data"] == {"flags": 64}
    assert channel.send.await_args.kwargs["content"] == "<@&40>"
    assert channel.send.await_args.kwargs["allowed_mentions"].to_dict() == {"roles": [40], "parse": []}
    assert all(child.disabled for child in channel.send.await_args.kwargs["view"].children)
    service.sync.assert_awaited_once_with(published)
    interaction.delete_original_response.assert_awaited_once()
    message.delete.assert_not_awaited()
    interaction.followup.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_publication_failure_releases_draft_and_removes_disabled_card(persona):
    service, existing = _availability(persona)
    member = _member(300, (30,))
    member.guild.get_role = {30: SimpleNamespace(id=30), 40: SimpleNamespace(id=40, mention="<@&40>")}.get
    member._user.name = "Instructor"
    member._user.global_name = None
    member._user.avatar = SimpleNamespace(url="https://x.test/avatar.png")
    interaction = _interaction(member)
    draft = _offer(status="publishing", message_id=None)
    service.repository.create.return_value = draft
    service.repository.publish.side_effect = RuntimeError("Database unavailable")
    message = SimpleNamespace(id=100, delete=AsyncMock())
    existing.destination_channel = AsyncMock(
        return_value=SimpleNamespace(
            id=50,
            send=AsyncMock(return_value=message),
        )
    )
    await service.create(interaction, start=draft.slots[0].starts_at, minutes=60, count=3, comments="")
    service.repository.fail_publication.assert_awaited_once_with(draft)
    message.delete.assert_awaited_once()
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_booking_validates_roles_again_and_does_not_send_extra_success_message(persona):
    service, _ = _availability(persona)
    member = _member(200, (40,))
    member.guild.get_member = Mock(return_value=_member(300, (30,)))
    offer = _offer()
    service.repository.get.return_value = offer
    service.repository.reserve.return_value = offer
    service.sync = AsyncMock()
    interaction = _interaction(member)
    await service.reserve(interaction, offer.id, offer.slots[0].id, FtpTrainingPhase.PHASE_1, "Texto")
    service.repository.reserve.assert_awaited_once()
    service.sync.assert_awaited_once_with(offer)
    interaction.response.defer.assert_awaited_once_with(invisible=True)
    interaction.delete_original_response.assert_not_awaited()
    interaction.original_response.assert_not_awaited()
    interaction.response.send_message.assert_not_awaited()
    member.guild.get_member.return_value = _member(300, (80,))
    service.repository.reserve.reset_mock()
    await service.reserve(_interaction(member), offer.id, offer.slots[1].id, FtpTrainingPhase.PHASE_1, "")
    service.repository.reserve.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_button_edits_offer_card_and_never_deletes_it(persona):
    service, _ = _availability(persona)
    offer = _offer()
    reserved = offer.slots[0].model_copy(
        update={
            "status": "reserved",
            "trainee_id": 200,
            "phase": FtpTrainingPhase.PHASE_2,
        }
    )
    closed = offer.model_copy(
        update={
            "status": "closed",
            "slots": (
                reserved,
                *(slot.model_copy(update={"status": "closed"}) for slot in offer.slots[1:]),
            ),
        }
    )
    service.repository.by_message.return_value = offer
    service.repository.close.return_value = closed
    message, _, _ = _discord_sync(service, closed)
    interaction = _interaction(_member(300, (30,)))
    interaction.message = message
    interaction.client = SimpleNamespace(ftp_availability_service=service)
    with _pycord_response(interaction, discord.InteractionType.component) as adapter:
        await AvailabilityControlsView(offer).close_button.callback(interaction)
    ack = adapter.create_interaction_response.await_args.kwargs
    assert ack["type"] == discord.InteractionResponseType.deferred_message_update.value
    service.repository.close.assert_awaited_once_with(offer.guild_id, offer.id)
    message.edit.assert_awaited_once()
    edit = message.edit.await_args.kwargs
    assert "Disponibilidad cerrada" in edit["embed"].description
    assert edit["embed"].color == discord.Color.light_grey()
    assert "<@200> · Fase 2" in edit["embed"].fields[0].value
    assert all(child.disabled for child in edit["view"].children)
    assert edit["allowed_mentions"].to_dict() == {"parse": []}
    message.delete.assert_not_awaited()
    interaction.delete_original_response.assert_not_awaited()
    interaction.original_response.assert_not_awaited()
    interaction.followup.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_reservation_modal_keeps_offer_card_and_other_free_blocks(persona):
    service, _ = _availability(persona)
    offer = _offer()
    reserved = offer.slots[0].model_copy(
        update={
            "status": "reserved",
            "trainee_id": 200,
            "phase": FtpTrainingPhase.PHASE_1,
            "notification_due": True,
        }
    )
    booked = offer.model_copy(update={"slots": (reserved, *offer.slots[1:])})
    message, _, _ = _discord_sync(service, booked)
    service.repository.reserve.return_value = booked
    member = _member(200, (40,))
    member.guild.get_member = Mock(return_value=_member(300, (30,)))
    interaction = _interaction(member)
    interaction.message = message
    modal = ReservationModal(service, 200, offer, offer.slots[0])
    modal.phase._selected_values = ["phase_1"]
    modal.phase._interaction = SimpleNamespace(data={})
    with _pycord_response(interaction, discord.InteractionType.modal_submit) as adapter:
        await modal.callback(interaction)
    assert (
        adapter.create_interaction_response.await_args.kwargs["type"]
        == discord.InteractionResponseType.deferred_message_update.value
    )
    edit = message.edit.await_args.kwargs
    assert "<@200> · Fase 1" in edit["embed"].fields[0].value
    select = next(child for child in edit["view"].children if isinstance(child, discord.ui.Select))
    assert [item.label for item in select.options] == ["Bloque 2", "Bloque 3"]
    message.reply.assert_awaited_once()
    message.delete.assert_not_awaited()
    interaction.delete_original_response.assert_not_awaited()
    interaction.original_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_error_is_private_without_deleting_card(persona):
    service, _ = _availability(persona)
    offer = _offer()
    service.repository.by_message.return_value = offer
    service.repository.close.side_effect = RuntimeError("Database unavailable")
    interaction = _interaction(_member(300, (30,)))
    with _pycord_response(interaction, discord.InteractionType.component):
        await service.close(interaction)
    assert interaction.followup.send.await_args.kwargs["ephemeral"] is True
    interaction.delete_original_response.assert_not_awaited()
    interaction.original_response.assert_not_awaited()


@pytest.mark.parametrize("ephemeral,same_source", [(False, True), (False, False), (True, True)])
@pytest.mark.asyncio
async def test_confirmation_cleanup_rejects_public_responses_and_source_message(ephemeral, same_source):
    interaction = _interaction()
    interaction.original_response.return_value = SimpleNamespace(
        id=interaction.message.id if same_source else 101,
        flags=discord.MessageFlags(ephemeral=ephemeral),
    )
    await clear_ephemeral_confirmation(interaction)
    interaction.delete_original_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmation_cleanup_only_deletes_separate_private_response():
    interaction = _interaction()
    await clear_ephemeral_confirmation(interaction)
    interaction.delete_original_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_cleanup_guard_also_protects_against_pycord_default_component_defer():
    interaction = _interaction()
    interaction.message.flags = discord.MessageFlags()
    with _pycord_response(interaction, discord.InteractionType.component):
        # Regression: ephemeral=True alone still points at the component's public card.
        await interaction.response.defer(ephemeral=True)
        await clear_ephemeral_confirmation(interaction)
    assert interaction.original_response.return_value is interaction.message
    interaction.delete_original_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_deleted_card_closes_only_free_slots_and_stops_sync_retry(persona):
    service, _ = _availability(persona)
    offer = _offer()
    _, channel, _ = _discord_sync(service, offer)
    response = SimpleNamespace(status=404, reason="Not Found")
    channel.fetch_message.side_effect = discord.NotFound(response, "Unknown Message")
    closed = offer.model_copy(update={"status": "closed", "revision": 1})
    service.repository.close.return_value = closed
    await service.sync(offer)
    service.repository.close.assert_awaited_once_with(offer.guild_id, offer.id)
    service.repository.acknowledge.assert_awaited_once_with(closed)


@pytest.mark.asyncio
async def test_notification_retry_does_not_duplicate_a_recent_confirmed_send(persona):
    service, _ = _availability(persona)
    offer = _offer()
    slot = offer.slots[0].model_copy(
        update={
            "status": "reserved",
            "trainee_id": 200,
            "phase": FtpTrainingPhase.PHASE_1,
            "notification_due": True,
        }
    )
    offer = offer.model_copy(update={"slots": (slot,)})
    message, channel, _ = _discord_sync(service, offer)

    async def history(**_):
        yield SimpleNamespace(author=SimpleNamespace(id=999), nonce=slot.id.hex[:25])

    channel.history = history
    await service.sync(offer)
    message.reply.assert_not_awaited()
    service.repository.notified.assert_awaited_once_with(slot.id)


@pytest.mark.asyncio
async def test_shared_scheduler_registration_and_config_disable(persona):
    service, _ = _availability(persona)
    scheduler = SimpleNamespace(register=Mock(), unregister=Mock())
    bundle = SimpleNamespace(
        snapshot=SimpleNamespace(guild_id=1), settings=SimpleNamespace(ftp_request_options=object())
    )
    await service.configure_maintenance(scheduler, bundle)
    assert scheduler.register.call_args.args[0] == "ftp-availability:1"
    assert scheduler.register.call_args.kwargs["interval_seconds"] == 60
    service.repository.expire.return_value = 0
    offer = _offer()
    service.repository.due.return_value = (offer,)
    service.sync = AsyncMock()
    await service.maintain(1)
    service.sync.assert_awaited_once_with(offer)
    bundle.settings.ftp_request_options = None
    await service.configure_maintenance(scheduler, bundle)
    scheduler.unregister.assert_called_once_with("ftp-availability:1")


@pytest.fixture
async def availability_db(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires TEST_DATABASE_URL with an isolated PostgreSQL database")
    monkeypatch.setenv("DATABASE_URL", url)
    await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")
    client = PostgresClient(PostgresClientConfig(url=url))
    await client.open()
    guild_id = 975432101234567820
    await client.execute(discord_guilds.delete().where(discord_guilds.c.discord_id == guild_id))
    await client.execute(discord_guilds.insert().values(discord_id=guild_id, name="Availability QA"))
    try:
        yield AvailabilityRepository(client), guild_id
    finally:
        await client.execute(discord_guilds.delete().where(discord_guilds.c.discord_id == guild_id))
        await client.close()


async def _create(repository, guild_id, *, interaction_id=99, start=None, count=3, instructor_id=300):
    return await repository.create(
        guild_id=guild_id,
        channel_id=50,
        interaction_id=interaction_id,
        instructor_id=instructor_id,
        start=start or datetime.now(UTC) + timedelta(hours=1),
        minutes=60,
        count=count,
        comments="Prueba",
    )


@pytest.mark.asyncio
async def test_repository_idempotent_multiple_offers_overlap_and_isolation(availability_db):
    repository, guild_id = availability_db
    first = await _create(repository, guild_id)
    assert (await _create(repository, guild_id)).id == first.id
    with pytest.raises(ValueError, match="cruzan"):
        await _create(repository, guild_id, interaction_id=100)
    second = await _create(repository, guild_id, interaction_id=101, start=first.slots[-1].ends_at)
    assert second.id != first.id
    with pytest.raises(LookupError):
        await repository.get(guild_id + 1, first.id)
    first = await repository.publish(first, 100)
    assert (await repository.by_message(guild_id, 50, 100)).id == first.id
    with pytest.raises(LookupError):
        await repository.by_message(guild_id, 51, 100)
    with pytest.raises(LookupError):
        await repository.reserve(
            guild_id + 1,
            first.id,
            first.slots[0].id,
            trainee_id=200,
            phase=FtpTrainingPhase.PHASE_1,
            comments="",
        )


@pytest.mark.asyncio
async def test_parallel_offer_creation_cannot_overlap(availability_db):
    repository, guild_id = availability_db
    result = await asyncio.gather(
        _create(repository, guild_id, interaction_id=110),
        _create(repository, guild_id, interaction_id=111),
        return_exceptions=True,
    )
    assert sum(isinstance(item, AvailabilityOffer) for item in result) == 1
    assert sum(isinstance(item, ValueError) for item in result) == 1


@pytest.mark.asyncio
async def test_atomic_reservation_creates_exactly_one_assigned_request_and_survives_restart(availability_db):
    repository, guild_id = availability_db
    offer = await repository.publish(await _create(repository, guild_id), 100)
    results = await asyncio.gather(
        *(
            repository.reserve(
                guild_id,
                offer.id,
                offer.slots[0].id,
                trainee_id=user,
                phase=FtpTrainingPhase.PHASE_2,
                comments="Comentario",
            )
            for user in (200, 201)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ValueError) for result in results) == 1
    restored = await AvailabilityRepository(repository.client).get(guild_id, offer.id)
    booked = restored.slots[0]
    assert booked.status == "reserved" and booked.trainee_id in (200, 201)
    assert booked.notification_due is True
    rows = await repository.client.fetch_all(sa.select(requests).where(requests.c.guild_id == guild_id))
    assert len(rows) == 1 and rows[0]["id"] == booked.request_id
    assert rows[0]["status"] == "claimed" and rows[0]["claimed_by_user_id"] == 300
    assert rows[0]["reason"] == "Comentario"
    assert rows[0]["scheduled_for"] == booked.starts_at
    assert rows[0]["message_id"] is None
    # Closing only affects free blocks, never an assigned evaluation.
    closed = await repository.close(guild_id, offer.id)
    assert [slot.status for slot in closed.slots] == ["reserved", "closed", "closed"]
    with pytest.raises(ValueError):
        await repository.reserve(
            guild_id, offer.id, offer.slots[1].id, trainee_id=201, phase=FtpTrainingPhase.PHASE_1, comments=""
        )


@pytest.mark.asyncio
async def test_expiry_end_boundary_preserves_bookings_and_retries_survive_restart(availability_db):
    repository, guild_id = availability_db
    offer = await repository.publish(await _create(repository, guild_id), 100)
    booked = await repository.reserve(
        guild_id, offer.id, offer.slots[1].id, trainee_id=200, phase=FtpTrainingPhase.PHASE_1, comments=""
    )
    assert await repository.expire(guild_id, offer.slots[0].ends_at - timedelta(seconds=1)) == 0
    assert await repository.expire(guild_id, offer.slots[0].ends_at) == 1
    assert await repository.expire(guild_id, offer.slots[-1].ends_at) == 1
    current = await repository.get(guild_id, offer.id)
    assert [slot.status for slot in current.slots] == ["expired", "reserved", "expired"]
    assert await repository.expire(guild_id, offer.slots[-1].ends_at) == 0
    # Older acknowledgements cannot drop a newer pending render.
    await repository.acknowledge(booked)
    due = await repository.due(guild_id, offer.slots[-1].ends_at)
    assert [item.id for item in due] == [offer.id]
    retry_at = datetime.now(UTC) + timedelta(days=10)
    await repository.acknowledge(current, retry_at)
    assert await repository.due(guild_id, retry_at - timedelta(seconds=1)) == ()
    assert len(await AvailabilityRepository(repository.client).due(guild_id, retry_at)) == 1
    await repository.acknowledge(current)
    assert await repository.due(guild_id, retry_at) == ()


@pytest.mark.asyncio
async def test_stale_selector_cannot_book_after_slot_end_and_failed_draft_releases_time(availability_db):
    repository, guild_id = availability_db
    draft = await _create(repository, guild_id)
    await repository.fail_publication(draft)
    offer = await repository.publish(await _create(repository, guild_id, interaction_id=101), 100)
    with pytest.raises(ValueError, match="venció"):
        await repository.reserve(
            guild_id,
            offer.id,
            offer.slots[0].id,
            trainee_id=200,
            phase=FtpTrainingPhase.PHASE_1,
            comments="",
            now=offer.slots[0].ends_at,
        )
    draft = await _create(repository, guild_id, interaction_id=102, instructor_id=301)
    await repository.client.execute(
        offers.update()
        .where(offers.c.id == draft.id)
        .values(
            created_at=datetime.now(UTC) - timedelta(minutes=16),
        )
    )
    await repository.expire(guild_id, datetime.now(UTC))
    assert (await repository.get(guild_id, draft.id)).status == "failed"
