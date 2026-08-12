from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import discord
import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError

from arrest_report_agent.config import (
    AccessPolicyKey,
    AccessPolicyMode,
    AccessPolicySnapshot,
    AppSettings,
    GuildAgentConfig,
    GuildConfigSnapshot,
    GuildFeatureSnapshot,
    GuildRuntimeSettings,
)
from arrest_report_agent.config.database import POLICY_SUBJECT_TYPES
from arrest_report_agent.infrastructure.postgres import PostgresClient, PostgresClientConfig
from arrest_report_agent.infrastructure.postgres.schema import discord_guilds
from arrest_report_agent.pagers.discord import (
    PagerCog,
    PagerControlsView,
    PagerCreateModal,
    PagerEditModal,
    PagerService,
    PagerValidationError,
    _recipient_header,
)
from arrest_report_agent.pagers.models import Pager, PagerResponder, PagerStatus, UserPager
from arrest_report_agent.pagers.repository import PagerRepository


def _snapshot(settings: AppSettings, *, enabled: bool) -> GuildConfigSnapshot:
    policies = {
        key: AccessPolicySnapshot(
            key=key,
            subject_type=POLICY_SUBJECT_TYPES[key],
            mode=(
                AccessPolicyMode.RESTRICTED
                if enabled
                and key
                in {
                    AccessPolicyKey.PAGER_CHANNELS,
                    AccessPolicyKey.PAGER_CREATOR_ROLES,
                    AccessPolicyKey.PAGER_RECIPIENT_ROLES,
                    AccessPolicyKey.PAGER_CLOSER_ROLES,
                }
                else AccessPolicyMode.DISABLED
            ),
            ids=(
                (100,)
                if enabled and key is AccessPolicyKey.PAGER_CHANNELS
                else (200,)
                if enabled
                and key
                in {
                    AccessPolicyKey.PAGER_CREATOR_ROLES,
                    AccessPolicyKey.PAGER_CLOSER_ROLES,
                }
                else (300, 301)
                if enabled and key is AccessPolicyKey.PAGER_RECIPIENT_ROLES
                else ()
            ),
        )
        for key in AccessPolicyKey
    }
    agent = GuildAgentConfig(
        model=settings.openai_model,
        reasoning_effort=settings.openai_reasoning_effort,
        review_timeout_seconds=settings.review_timeout_seconds,
        max_agent_turns=settings.max_agent_turns,
        max_images_per_review=settings.max_images_per_review,
        max_image_bytes=settings.max_image_bytes,
        max_files_per_review=settings.max_files_per_review,
        max_file_bytes=settings.max_file_bytes,
        max_total_file_bytes=settings.max_total_file_bytes,
        thread_context_max_messages=settings.thread_context_max_messages,
        channel_context_max_messages=settings.channel_context_max_messages,
        thread_context_max_chars=settings.thread_context_max_chars,
        thread_context_max_message_chars=settings.thread_context_max_message_chars,
        thread_context_max_historical_images=settings.thread_context_max_historical_images,
        thread_context_max_historical_files=settings.thread_context_max_historical_files,
        thread_auto_archive_minutes=settings.thread_auto_archive_minutes,  # type: ignore[arg-type]
    )
    return GuildConfigSnapshot(
        guild_id=1,
        revision_id=uuid4(),
        revision=1,
        agent=agent,
        policies=policies,
        features={
            "pagers": GuildFeatureSnapshot(
                key="pagers",
                enabled=enabled,
                schema_version=1,
                payload={"starting_number": 92},
            )
        },
        persona_revision_id=uuid4(),
        persona_id="walter_vigil",
        persona_revision=20,
    )


def _pager(status: PagerStatus = PagerStatus.ACTIVE) -> Pager:
    now = datetime.now(UTC)
    return Pager(
        id=uuid4(),
        guild_id=1,
        channel_id=100,
        message_id=500,
        number=92,
        creator_user_id=10,
        creator_callsign="241WS",
        recipient_role_ids=(300, 301),
        location="Ganton",
        situation="Unidad solicita apoyo.",
        status=status,
        resolution="Situación controlada." if status.terminal else None,
        closed_by_user_id=10 if status.terminal else None,
        created_at=now,
        updated_at=now,
        closed_at=now if status.terminal else None,
        responders=(PagerResponder(user_id=11, callsign="242WS", created_at=now, updated_at=now),),
    )


def test_pager_domain_builds_stable_number_link_and_relationships() -> None:
    pager = _pager()

    assert pager.display_number == "00092"
    assert pager.jump_url == "https://discord.com/channels/1/100/500"
    assert UserPager(pager=pager, creator=True, responder_callsign="241WS").relationship == (
        "Emisor y respondiente"
    )
    assert PagerStatus.CONCLUDED.terminal
    assert not PagerStatus.ACTIVE.terminal


@pytest.mark.asyncio
async def test_pager_embed_and_persistent_controls_are_deterministic(
    settings: AppSettings,
    persona,
) -> None:
    runtime = GuildRuntimeSettings(settings, _snapshot(settings, enabled=True))
    service = PagerService(
        SimpleNamespace(),
        SimpleNamespace(),
        runtime,
        persona,
        enabled=True,
    )

    active = service.build_embed(_pager())
    terminal = PagerControlsView(disabled=True)

    assert active.title == "📟 Pager #00092"
    assert active.description == "📻 Activo"
    assert all(field.name != "Destinatarios" for field in active.fields)
    assert _recipient_header(_pager()) == ("📣 **Pager dirigido a**\n> <@&300> · <@&301>\n\n\u200b")
    assert any(field.name == "Unidades que responden · 1" for field in active.fields)
    assert {child.custom_id for child in terminal.children} == {
        "pager:v1:respond",
        "pager:v1:withdraw",
        "pager:v1:edit",
        "pager:v1:conclude",
        "pager:v1:cancel",
    }
    assert all(child.disabled for child in terminal.children)


def test_pager_recipient_can_be_notified_with_effective_channel_permission(
    settings: AppSettings,
    persona,
) -> None:
    runtime = GuildRuntimeSettings(settings, _snapshot(settings, enabled=True))
    service = PagerService(SimpleNamespace(), SimpleNamespace(), runtime, persona, enabled=True)
    role = SimpleNamespace(id=300, mentionable=False)
    bot_member = SimpleNamespace(id=999)
    guild = SimpleNamespace(me=bot_member, get_role=lambda role_id: role if role_id == 300 else None)
    channel = SimpleNamespace(
        guild=guild,
        permissions_for=lambda member: SimpleNamespace(
            mention_everyone=member is bot_member,
        ),
    )

    assert service.validate_recipients(channel, (300,)) == (role,)


@pytest.mark.asyncio
async def test_pager_modal_accepts_multiple_recipients(settings: AppSettings, persona) -> None:
    runtime = GuildRuntimeSettings(settings, _snapshot(settings, enabled=True))
    service = PagerService(SimpleNamespace(), SimpleNamespace(), runtime, persona, enabled=True)
    roles = {role_id: SimpleNamespace(id=role_id, name=f"Role {role_id}") for role_id in (300, 301)}
    guild = SimpleNamespace(get_role=roles.get)
    member = SimpleNamespace(id=10, guild=guild)

    modal = PagerCreateModal(service, member, SimpleNamespace())

    assert modal.recipient.min_values == 1
    assert modal.recipient.max_values == 2


@pytest.mark.asyncio
async def test_pager_edit_modal_is_prepopulated(settings: AppSettings, persona) -> None:
    runtime = GuildRuntimeSettings(settings, _snapshot(settings, enabled=True))
    service = PagerService(SimpleNamespace(), SimpleNamespace(), runtime, persona, enabled=True)
    roles = {role_id: SimpleNamespace(id=role_id, name=f"Role {role_id}") for role_id in (300, 301)}
    guild = SimpleNamespace(get_role=roles.get)
    member = SimpleNamespace(id=10, guild=guild)
    pager = _pager()

    modal = PagerEditModal(service, pager, member, SimpleNamespace())

    assert modal.callsign.value == pager.creator_callsign
    assert modal.location.value == pager.location
    assert modal.situation.value == pager.situation
    assert {int(option.value) for option in modal.recipient.options if option.default} == {300, 301}


def test_pager_recipient_requires_role_or_effective_mention_permission(
    settings: AppSettings,
    persona,
) -> None:
    runtime = GuildRuntimeSettings(settings, _snapshot(settings, enabled=True))
    service = PagerService(SimpleNamespace(), SimpleNamespace(), runtime, persona, enabled=True)
    role = SimpleNamespace(id=300, mentionable=False)
    guild = SimpleNamespace(
        me=SimpleNamespace(id=999),
        get_role=lambda role_id: role if role_id == 300 else None,
    )
    channel = SimpleNamespace(
        guild=guild,
        permissions_for=lambda _member: SimpleNamespace(mention_everyone=False),
    )

    with pytest.raises(PagerValidationError):
        service.validate_recipients(channel, (300,))


def test_pager_rejects_duplicate_or_empty_recipients() -> None:
    payload = _pager().model_dump()
    payload["recipient_role_ids"] = ()
    with pytest.raises(ValidationError):
        Pager.model_validate(payload)

    payload["recipient_role_ids"] = (300, 300)
    with pytest.raises(ValidationError, match="unique"):
        Pager.model_validate(payload)


@pytest.mark.asyncio
async def test_pager_emitter_cannot_register_as_responder(
    settings: AppSettings,
    persona,
) -> None:
    messages: list[dict[str, object]] = []

    async def send_message(**kwargs: object) -> None:
        messages.append(kwargs)

    async def get(_pager_id):
        return _pager()

    interaction = SimpleNamespace(
        user=SimpleNamespace(id=10),
        response=SimpleNamespace(send_message=send_message),
        followup=SimpleNamespace(send=send_message),
        guild_id=1,
        channel_id=100,
        message=None,
    )
    runtime = GuildRuntimeSettings(settings, _snapshot(settings, enabled=True))
    service = PagerService(
        SimpleNamespace(),
        SimpleNamespace(get=get),
        runtime,
        persona,
        enabled=True,
    )

    await service.respond(interaction, _pager().id, "241WS")

    assert len(messages) == 1
    assert messages[0]["ephemeral"] is True
    assert service.ui.creator_cannot_respond in messages[0]["embed"].description


@pytest.mark.asyncio
async def test_successful_pager_response_only_updates_public_message(
    settings: AppSettings,
    persona,
) -> None:
    deferred: list[bool] = []
    followups: list[dict[str, object]] = []
    original_response_deletions = 0
    pager = _pager()

    async def defer() -> None:
        deferred.append(False)

    async def send(**kwargs: object) -> None:
        followups.append(kwargs)

    async def delete_original_response() -> None:
        nonlocal original_response_deletions
        original_response_deletions += 1

    async def get(_pager_id):
        return pager

    async def upsert_responder(_pager_id, _user_id, _callsign):
        return pager

    async def refresh_message(_pager: Pager) -> None:
        return None

    interaction = SimpleNamespace(
        user=SimpleNamespace(id=11),
        response=SimpleNamespace(defer=defer),
        followup=SimpleNamespace(send=send),
        delete_original_response=delete_original_response,
        guild_id=1,
        channel_id=100,
        message=None,
    )
    runtime = GuildRuntimeSettings(settings, _snapshot(settings, enabled=True))
    service = PagerService(
        SimpleNamespace(),
        SimpleNamespace(get=get, upsert_responder=upsert_responder),
        runtime,
        persona,
        enabled=True,
    )
    service.refresh_message = refresh_message  # type: ignore[method-assign]

    await service.respond(interaction, pager.id, "242WS")

    assert deferred == [False]
    assert followups == []
    assert original_response_deletions == 0


@pytest.mark.asyncio
async def test_editing_pager_updates_public_message_without_confirmation(
    settings: AppSettings,
    persona,
) -> None:
    deferred = 0
    followups: list[dict[str, object]] = []
    refreshed: list[Pager] = []
    active = _pager()
    edited = active.model_copy(
        update={
            "creator_callsign": "245WS",
            "recipient_role_ids": (301,),
            "location": "Dillimore",
            "situation": "Apoyo vial requerido.",
        }
    )

    async def defer() -> None:
        nonlocal deferred
        deferred += 1

    async def send(**kwargs: object) -> None:
        followups.append(kwargs)

    async def get(_pager_id):
        return active

    async def update_active(_pager_id, **kwargs):
        assert kwargs == {
            "creator_callsign": "245WS",
            "recipient_role_ids": (301,),
            "location": "Dillimore",
            "situation": "Apoyo vial requerido.",
        }
        return edited

    async def refresh_message(pager: Pager) -> None:
        refreshed.append(pager)

    member = discord.Member(
        data={"user": {"id": str(active.creator_user_id)}, "roles": []},
        guild=SimpleNamespace(),
        state=SimpleNamespace(store_user=lambda data: SimpleNamespace(id=int(data["id"]))),
    )
    roles = {role_id: SimpleNamespace(id=role_id, mentionable=True) for role_id in active.recipient_role_ids}
    channel = SimpleNamespace(guild=SimpleNamespace(get_role=roles.get))
    interaction = SimpleNamespace(
        user=member,
        response=SimpleNamespace(defer=defer),
        followup=SimpleNamespace(send=send),
        guild_id=active.guild_id,
        channel_id=active.channel_id,
        message=None,
    )
    runtime = GuildRuntimeSettings(settings, _snapshot(settings, enabled=True))
    service = PagerService(
        SimpleNamespace(),
        SimpleNamespace(get=get, update_active=update_active),
        runtime,
        persona,
        enabled=True,
    )
    service.refresh_message = refresh_message  # type: ignore[method-assign]

    await service.edit(
        interaction,
        active.id,
        channel=channel,
        callsign="245WS",
        recipient_role_ids=(301,),
        location="Dillimore",
        situation="Apoyo vial requerido.",
    )

    assert deferred == 1
    assert refreshed == [edited]
    assert followups == []


@pytest.mark.asyncio
async def test_closing_pager_edits_public_message_without_deleting_it(
    settings: AppSettings,
    persona,
) -> None:
    deferred = 0
    followups: list[dict[str, object]] = []
    refreshed: list[Pager] = []
    original_response_deletions = 0
    active = _pager()
    concluded = active.model_copy(
        update={
            "status": PagerStatus.CONCLUDED,
            "resolution": "Situación controlada.",
            "closed_by_user_id": active.creator_user_id,
            "closed_at": datetime.now(UTC),
        }
    )

    async def defer() -> None:
        nonlocal deferred
        deferred += 1

    async def send(**kwargs: object) -> None:
        followups.append(kwargs)

    async def delete_original_response() -> None:
        nonlocal original_response_deletions
        original_response_deletions += 1

    async def get(_pager_id):
        return active

    async def close(_pager_id, **_kwargs):
        return concluded, True

    async def refresh_message(pager: Pager) -> None:
        refreshed.append(pager)

    member = discord.Member(
        data={"user": {"id": str(active.creator_user_id)}, "roles": []},
        guild=SimpleNamespace(),
        state=SimpleNamespace(store_user=lambda data: SimpleNamespace(id=int(data["id"]))),
    )
    interaction = SimpleNamespace(
        user=member,
        response=SimpleNamespace(defer=defer),
        followup=SimpleNamespace(send=send),
        delete_original_response=delete_original_response,
        guild_id=active.guild_id,
        channel_id=active.channel_id,
        message=None,
    )
    runtime = GuildRuntimeSettings(settings, _snapshot(settings, enabled=True))
    service = PagerService(
        SimpleNamespace(),
        SimpleNamespace(get=get, close=close),
        runtime,
        persona,
        enabled=True,
    )
    service.refresh_message = refresh_message  # type: ignore[method-assign]

    await service.close(
        interaction,
        active.id,
        status=PagerStatus.CONCLUDED,
        resolution="Situación controlada.",
    )

    assert deferred == 1
    assert refreshed == [concluded]
    assert followups == []
    assert original_response_deletions == 0


def test_pager_commands_are_root_commands() -> None:
    commands = (
        PagerCog.pager,
        PagerCog.active_pagers,
        PagerCog.my_pagers,
        PagerCog.info_pager,
    )

    assert [command.name for command in commands] == [
        "pager",
        "pager-activos",
        "mis-pagers",
        "info-pager",
    ]
    assert all(command.parent is None for command in commands)


def test_enabled_pagers_require_restricted_complete_configuration(settings: AppSettings) -> None:
    snapshot = _snapshot(settings, enabled=True)

    assert snapshot.policy(AccessPolicyKey.PAGER_CHANNELS).ids == (100,)
    assert GuildRuntimeSettings(settings, snapshot).pager_starting_number == 92

    payload = snapshot.model_dump()
    payload["policies"][AccessPolicyKey.PAGER_CHANNELS]["mode"] = AccessPolicyMode.DISABLED
    payload["policies"][AccessPolicyKey.PAGER_CHANNELS]["ids"] = ()
    with pytest.raises(ValidationError, match="pager_channels"):
        GuildConfigSnapshot.model_validate(payload)


@pytest.mark.asyncio
async def test_pager_repository_persists_responses_and_idempotent_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("requires TEST_DATABASE_URL with an isolated PostgreSQL database")
    monkeypatch.setenv("DATABASE_URL", database_url)
    await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    guild_id = 987654321012345678
    try:
        await client.execute(discord_guilds.delete().where(discord_guilds.c.discord_id == guild_id))
        await client.execute(discord_guilds.insert().values(discord_id=guild_id, name="Pager QA"))
        repository = PagerRepository(client)
        pager = await repository.create_publishing(
            guild_id=guild_id,
            channel_id=100,
            creator_user_id=10,
            creator_callsign="241WS",
            recipient_role_ids=(300, 301),
            location="Ganton",
            situation="Apoyo requerido.",
            starting_number=92,
        )
        assert pager.number == 92
        assert pager.recipient_role_ids == (300, 301)
        pager = await repository.publish(pager.id, 500)
        pager = await repository.update_active(
            pager.id,
            creator_callsign="245WS",
            recipient_role_ids=(301,),
            location="Dillimore",
            situation="Apoyo vial requerido.",
        )
        assert pager.creator_callsign == "245WS"
        assert pager.recipient_role_ids == (301,)
        assert pager.location == "Dillimore"
        assert pager.situation == "Apoyo vial requerido."
        with pytest.raises(ValueError, match="creator"):
            await repository.upsert_responder(pager.id, 10, "241WS")
        pager = await repository.upsert_responder(pager.id, 11, "242WS")
        pager = await repository.upsert_responder(pager.id, 11, "243WS")
        assert [(item.user_id, item.callsign) for item in pager.responders] == [(11, "243WS")]

        creator_entries, creator_total = await repository.list_for_user(
            guild_id,
            10,
            offset=0,
            limit=5,
        )
        responder_entries, responder_total = await repository.list_for_user(
            guild_id,
            11,
            offset=0,
            limit=5,
        )
        unrelated_entries, unrelated_total = await repository.list_for_user(
            guild_id,
            12,
            offset=0,
            limit=5,
        )

        assert creator_total == 1
        assert creator_entries[0].relationship == "Emisor"
        assert responder_total == 1
        assert responder_entries[0].relationship == "Respondiente"
        assert responder_entries[0].responder_callsign == "243WS"
        assert unrelated_total == 0
        assert unrelated_entries == ()

        closed, changed = await repository.close(
            pager.id,
            status=PagerStatus.CONCLUDED,
            resolution="Situación controlada.",
            actor_user_id=10,
        )
        repeated, changed_again = await repository.close(
            pager.id,
            status=PagerStatus.CONCLUDED,
            resolution="No debe reemplazar el resultado.",
            actor_user_id=10,
        )

        assert changed and not changed_again
        assert closed.status is PagerStatus.CONCLUDED
        assert repeated.resolution == "Situación controlada."
        restored = await repository.restore_active_after_delivery_failure(
            pager.id,
            expected_status=PagerStatus.CONCLUDED,
        )
        assert restored.status is PagerStatus.ACTIVE
        assert restored.resolution is None
    finally:
        await client.execute(discord_guilds.delete().where(discord_guilds.c.discord_id == guild_id))
        await client.close()
