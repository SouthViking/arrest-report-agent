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

from arrest_report_agent.config import FtpRequestOptions, FtpStatusOptions
from arrest_report_agent.ftp_requests import (
    ActiveFtpRequestError,
    FtpRequestRepository,
    FtpRequestStatus,
    FtpTrainingRequest,
)
from arrest_report_agent.ftp_requests.discord import (
    FtpRequestControlsView,
    FtpRequestService,
    build_ftp_request_embed,
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
        ftp_status_options=FtpStatusOptions(
            faction_id=3,
            supervisory_role_ids=(10,),
            mfto_role_ids=(20,),
            fto_role_ids=(30,),
            ftt_role_ids=(40,),
        ),
        ftp_request_options=FtpRequestOptions(
            channel_id=50,
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


def _request(status: FtpRequestStatus = FtpRequestStatus.PENDING) -> FtpTrainingRequest:
    return FtpTrainingRequest(
        id=uuid4(),
        guild_id=1,
        channel_id=50,
        message_id=100,
        requester_user_id=200,
        reason="Necesito practicar una detención vehicular completa.",
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
    assert service.can_request(_member(1, (30,)))  # FTO
    assert service.can_request(_member(1, (60,)))  # Command
    assert not service.can_request(_member(1, (999,)))

    assert service.can_claim(_member(1, (30,)))  # FTO
    assert service.can_claim(_member(1, (10,)))  # Supervisory
    assert service.can_claim(_member(1, (70,)))  # Executive
    assert not service.can_claim(_member(1, (40,)))  # FTT

    request = _request()
    assert service.can_cancel(_member(200, (40,)), request)
    assert service.can_cancel(_member(1, (60,)), request)
    assert service.can_cancel(_member(1, (70,)), request)
    assert not service.can_cancel(_member(1, (30,)), request)


def test_ftp_request_notifies_only_supervisory_mfto_and_fto(persona) -> None:
    service = _service(persona)
    roles = {
        role_id: SimpleNamespace(id=role_id)
        for role_id in (10, 20, 30, 40, 60, 70)
    }

    assert [role.id for role in service.notification_roles(SimpleNamespace(get_role=roles.get))] == [
        10,
        20,
        30,
    ]


@pytest.mark.asyncio
async def test_terminal_ftp_request_view_disables_both_actions() -> None:
    view = FtpRequestControlsView(disabled=True)

    assert len(view.children) == 2
    assert all(getattr(child, "disabled", False) for child in view.children)


def test_ftp_request_embed_is_neutral_and_disables_technical_ids() -> None:
    pending = build_ftp_request_embed(_request())
    claimed = build_ftp_request_embed(_request(FtpRequestStatus.CLAIMED))

    assert pending.title == "🎓 Solicitud de instrucción FTP"
    assert pending.description == "Necesito practicar una detención vehicular completa."
    assert "100" not in str(pending.to_dict())
    assert any(field.name == "Estado" and field.value == "Esperando instructor" for field in pending.fields)
    assert any(field.name == "Instructor asignado" and field.value == "<@300>" for field in claimed.fields)


def test_ftp_request_options_reject_overlap_between_management_roles() -> None:
    with pytest.raises(ValueError, match="unique"):
        FtpRequestOptions(
            channel_id=50,
            command_role_ids=(60,),
            executive_role_ids=(60,),
        )


@pytest.mark.asyncio
async def test_ftp_request_repository_serializes_creation_and_transitions(
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
        await client.execute(
            discord_guilds.insert().values(discord_id=guild_id, name="FTP requests QA")
        )
        repository = FtpRequestRepository(client)

        async def reserve():
            return await repository.create_publishing(
                guild_id=guild_id,
                channel_id=975432101234567802,
                requester_user_id=requester_id,
                reason="Necesito practicar una detención vehicular completa.",
            )

        outcomes = await asyncio.gather(reserve(), reserve(), return_exceptions=True)
        created = [outcome for outcome in outcomes if isinstance(outcome, FtpTrainingRequest)]
        blocked = [outcome for outcome in outcomes if isinstance(outcome, ActiveFtpRequestError)]
        assert len(created) == 1
        assert len(blocked) == 1

        request = await repository.publish(created[0].id, 975432101234567803)
        assert request.status is FtpRequestStatus.PENDING
        restored = await FtpRequestRepository(client).get_by_message(
            guild_id,
            request.channel_id,
            request.message_id or 0,
        )
        assert restored is not None and restored.id == request.id

        claims = await asyncio.gather(
            repository.claim(request.id, actor_user_id=975432101234567804),
            repository.claim(request.id, actor_user_id=975432101234567805),
        )
        assert sum(changed for _, changed in claims) == 1
        claimed = next(item for item, changed in claims if changed)
        assert claimed.status is FtpRequestStatus.CLAIMED

        another = await reserve()
        assert another.id != request.id
    finally:
        await client.execute(discord_guilds.delete().where(discord_guilds.c.discord_id == guild_id))
        await client.close()
