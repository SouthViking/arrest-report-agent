from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import discord
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from pydantic import ValidationError

from arrest_report_agent.application.maintenance import MaintenanceScheduler
from arrest_report_agent.config import FtpRequestOptions
from arrest_report_agent.ftp_requests.discord import build_ftp_info_embed, build_ftp_request_embed
from arrest_report_agent.ftp_requests.maintenance import FtpRequestMaintenance
from arrest_report_agent.ftp_requests.models import (
    FtpRequestStatus,
    FtpTrainingRequest,
)
from arrest_report_agent.ftp_requests.repository import FtpRequestRepository
from arrest_report_agent.infrastructure.postgres import PostgresClient, PostgresClientConfig
from arrest_report_agent.infrastructure.postgres.schema import discord_guilds, ftp_training_requests

NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)
OPTIONS = FtpRequestOptions(channel_id=50, command_role_ids=(60,), executive_role_ids=(70,))


def cancelled_request(**kwargs):
    return FtpTrainingRequest.model_validate({
        "id": uuid4(), "guild_id": 1, "channel_id": 50, "message_id": 100,
        "requester_user_id": 200, "reason": "Prefiero voz.", "training_phase": "phase_2",
        "requested_instructor_level": "general", "scheduled_for": NOW - timedelta(hours=6),
        "status": "cancelled", "created_at": NOW - timedelta(days=1), "updated_at": NOW,
        "resolved_at": NOW, "auto_cancelled_at": NOW, "expiry_sync_due_at": NOW, **kwargs,
    })


def setup_worker(options=OPTIONS, requests=None):
    requests = (cancelled_request(),) if requests is None else requests
    repository = SimpleNamespace(
        expire_pending=AsyncMock(return_value=requests), list_expiry_sync=AsyncMock(return_value=requests),
        set_expiry_sync_due=AsyncMock(),
    )
    bundle = SimpleNamespace(snapshot=SimpleNamespace(guild_id=1),
                             settings=SimpleNamespace(ftp_request_options=options))
    provider = SimpleNamespace(cached=AsyncMock(return_value=bundle))
    existing = discord.Embed().set_author(name="C. Ejemplo", icon_url="https://example.com/avatar.png")
    message = SimpleNamespace(author=SimpleNamespace(id=999), embeds=[existing], edit=AsyncMock())
    channel = Mock(spec=discord.TextChannel)
    channel.fetch_message = AsyncMock(return_value=message)
    guild = SimpleNamespace(get_channel=Mock(return_value=channel), fetch_channel=AsyncMock())
    bot = SimpleNamespace(get_guild=Mock(return_value=guild), user=SimpleNamespace(id=999))
    scheduler = MaintenanceScheduler(lambda: True)
    worker = FtpRequestMaintenance(bot, provider, repository, scheduler)
    return worker, repository, message, channel, bundle


async def test_expiry_cancels_then_updates_without_pings_and_preserves_author():
    worker, repository, message, _, _ = setup_worker()
    await worker.process_guild(1, now=NOW)
    worker.provider.cached.assert_awaited_once_with(1)
    repository.expire_pending.assert_awaited_once_with(1, cutoff=NOW - timedelta(hours=6), now=NOW)
    sent = message.edit.call_args.kwargs
    assert sent["content"] is None
    assert sent["allowed_mentions"].users is False
    assert sent["allowed_mentions"].roles is False
    assert all(item.disabled for item in sent["view"].children)
    assert sent["embed"].author.name == "C. Ejemplo"
    assert str(sent["embed"].author.icon_url) == "https://example.com/avatar.png"
    assert sent["embed"].color == discord.Color.dark_grey()
    text = str(sent["embed"].to_dict())
    assert "Cancelada automáticamente" in text and "Puedes crear otra" in text
    assert "Prefiero voz." in text and ":R>" in text
    assert "Cancelada por" not in text
    assert repository.set_expiry_sync_due.call_args.args[1] is None


async def test_expiry_edit_failure_is_retried_after_worker_restart_without_reopening(caplog):
    worker, repository, message, _, _ = setup_worker()
    request = repository.list_expiry_sync.return_value[0]
    message.edit.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "denied")
    await worker.process_guild(1, now=NOW)
    repository.set_expiry_sync_due.assert_awaited_once_with(request, NOW + timedelta(minutes=10))
    assert "ftp_expiry_card_sync_failed" in caplog.text
    repository.expire_pending.return_value = ()
    message.edit.side_effect = None
    restarted = FtpRequestMaintenance(
        worker.bot, worker.provider, repository, MaintenanceScheduler(lambda: True),
    )
    await restarted.process_guild(1, now=NOW + timedelta(minutes=10))
    assert repository.set_expiry_sync_due.call_args.args == (request, None)
    assert message.edit.await_count == 2


@pytest.mark.parametrize("operation", ["fetch", "edit", "channel"])
async def test_deleted_message_or_channel_stops_retrying_without_recreating(operation):
    worker, repository, message, channel, _ = setup_worker()
    error = discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "missing")
    if operation == "fetch":
        channel.fetch_message.side_effect = error
    elif operation == "edit":
        message.edit.side_effect = error
    else:
        worker.bot.get_guild.return_value.get_channel.return_value = None
        worker.bot.get_guild.return_value.fetch_channel.side_effect = error
    await worker.process_guild(1, now=NOW)
    assert repository.set_expiry_sync_due.call_args.args[1] is None
    channel.send.assert_not_called()


async def test_missing_guild_retries_and_foreign_author_is_not_edited():
    worker, repository, message, _, _ = setup_worker()
    message.author.id = 888
    await worker.process_guild(1, now=NOW)
    message.edit.assert_not_awaited()
    assert repository.set_expiry_sync_due.call_args.args[1] is not None
    worker.bot.get_guild.return_value = None
    await worker.process_guild(1, now=NOW)
    assert repository.set_expiry_sync_due.await_count == 2


async def test_expiry_handles_each_message_independently():
    first, second = cancelled_request(), cancelled_request(message_id=101)
    worker, repository, message, channel, _ = setup_worker(requests=(first, second))
    channel.fetch_message.side_effect = [RuntimeError("temporarily unavailable"), message]
    await worker.process_guild(1, now=NOW)
    assert repository.set_expiry_sync_due.call_args_list[0].args == (first, NOW + timedelta(minutes=10))
    assert repository.set_expiry_sync_due.call_args_list[1].args == (second, None)


async def test_disabled_expiry_still_reconciles_existing_cancellations():
    worker, repository, message, _, _ = setup_worker(OPTIONS.model_copy(update={"expiry_enabled": False}))
    await worker.process_guild(1, now=NOW)
    repository.expire_pending.assert_not_awaited()
    message.edit.assert_awaited_once()


async def test_disabled_module_does_no_work():
    worker, repository, message, _, bundle = setup_worker(None)
    await worker.configuration_updated(bundle)
    await worker.scheduler.process_once()
    await worker.process_guild(1, now=NOW)
    repository.expire_pending.assert_not_awaited()
    repository.list_expiry_sync.assert_not_awaited()
    message.edit.assert_not_awaited()


async def test_expiry_config_hot_reload_changes_job_interval_and_grace(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("arrest_report_agent.application.maintenance.monotonic", lambda: clock[0])
    worker, repository, _, _, bundle = setup_worker(requests=())
    await worker.configuration_updated(bundle)
    await worker.scheduler.process_once()
    clock[0] = 60
    await worker.scheduler.process_once()
    assert repository.expire_pending.await_count == 1
    bundle.settings.ftp_request_options = OPTIONS.model_copy(
        update={"expiry_check_minutes": 1, "expiry_grace_hours": 12},
    )
    await worker.configuration_updated(bundle)
    await worker.scheduler.process_once()
    args = repository.expire_pending.call_args.kwargs
    assert args["now"] - args["cutoff"] == timedelta(hours=12)
    clock[0] = 120
    await worker.scheduler.process_once()
    assert repository.expire_pending.await_count == 3
    bundle.settings.ftp_request_options = None
    await worker.configuration_updated(bundle)
    clock[0] = 5000
    await worker.scheduler.process_once()
    assert repository.expire_pending.await_count == 3


async def test_idle_expiry_is_quiet(caplog):
    worker, _, message, _, _ = setup_worker(requests=())
    await worker.process_guild(1, now=NOW)
    message.edit.assert_not_awaited()
    assert not caplog.records


def test_manual_cancel_rendering_is_unchanged():
    request = cancelled_request(auto_cancelled_at=None, expiry_sync_due_at=None, resolved_by_user_id=300)
    embed = build_ftp_request_embed(request)
    assert embed.color == discord.Color.red()
    assert any(field.name == "Cancelada por" and field.value == "<@300>" for field in embed.fields)
    assert "automáticamente" not in str(embed.to_dict())


def test_info_explains_dynamic_expiry_without_technical_details(persona):
    service = SimpleNamespace(request_options=OPTIONS, ui=persona.messages.interface.ftp_requests,
                              command_channel_mentions=lambda: "<#50>")
    text = str(build_ftp_info_embed(service).to_dict())
    assert "6 horas desde el horario solicitado" in text
    service.request_options = OPTIONS.model_copy(update={"expiry_grace_hours": 12})
    assert "12 horas" in str(build_ftp_info_embed(service).to_dict())
    service.request_options = OPTIONS.model_copy(update={"expiry_enabled": False})
    assert "automáticamente" not in str(build_ftp_info_embed(service).to_dict())


@pytest.mark.parametrize("field,value", [
    ("expiry_check_minutes", 0), ("expiry_check_minutes", 1441),
    ("expiry_grace_hours", 0), ("expiry_grace_hours", 169),
])
def test_expiry_configuration_bounds(field, value):
    with pytest.raises(ValidationError):
        FtpRequestOptions.model_validate({**OPTIONS.model_dump(), field: value})


@pytest.fixture
async def expiry_database(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires TEST_DATABASE_URL with isolated PostgreSQL")
    monkeypatch.setenv("DATABASE_URL", url)
    await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")
    client = PostgresClient(PostgresClientConfig(url=url))
    await client.open()
    guilds = (975432101234567810, 975432101234567811)
    for guild in guilds:
        await client.execute(discord_guilds.insert().values(discord_id=guild, name="Expiry QA"))
    try:
        yield client, FtpRequestRepository(client), guilds
    finally:
        await client.execute(discord_guilds.delete().where(discord_guilds.c.discord_id.in_(guilds)))
        await client.close()


async def insert_request(
    client, guild, *, scheduled_for=NOW - timedelta(hours=6), status="pending", **kwargs,
):
    request = cancelled_request(
        guild_id=guild, scheduled_for=scheduled_for, status=status, auto_cancelled_at=None,
        expiry_sync_due_at=None, resolved_at=None, **kwargs,
    )
    await client.execute(ftp_training_requests.insert().values(**request.model_dump()))
    return request


async def test_expiry_database_deadline_guild_scope_legacy_and_multiple_requests(expiry_database):
    client, repository, (guild, other) = expiry_database
    boundary = await insert_request(client, guild, message_id=1)
    await insert_request(
        client, guild, message_id=2, scheduled_for=NOW - timedelta(hours=6) + timedelta(seconds=1),
    )
    await insert_request(client, guild, message_id=3, scheduled_for=NOW + timedelta(days=1))
    legacy = await insert_request(client, guild, message_id=4, scheduled_for=None)
    await insert_request(client, other, message_id=5)
    for index, status in enumerate(("claimed", "cancelled", "failed", "publishing"), start=6):
        await insert_request(client, guild, message_id=index, status=status)
    cancelled = await repository.expire_pending(guild, cutoff=NOW - timedelta(hours=6), now=NOW)
    assert {row.id for row in cancelled} == {boundary.id, legacy.id}
    assert all(row.status is FtpRequestStatus.CANCELLED and row.auto_cancelled_at == NOW for row in cancelled)
    assert all(row.resolved_by_user_id is None for row in cancelled)
    assert await repository.expire_pending(guild, cutoff=NOW - timedelta(hours=6), now=NOW) == ()
    pending, total = await repository.list_pending(guild, offset=0, limit=10)
    assert total == 2 and {row.message_id for row in pending} == {2, 3}
    _, foreign_total = await repository.list_pending(other, offset=0, limit=10)
    assert foreign_total == 1
    restored = await repository.restore_pending(boundary.id, expected_status=FtpRequestStatus.CANCELLED)
    assert restored.status is FtpRequestStatus.CANCELLED


async def test_expiry_database_persistent_retry_markers_and_bounded_batches(expiry_database):
    client, repository, (guild, _) = expiry_database
    for message_id in range(1, 4):
        await insert_request(client, guild, message_id=message_id)
    first = await repository.expire_pending(guild, cutoff=NOW - timedelta(hours=6), now=NOW, limit=1)
    assert len(first) == 1
    # A new repository observes durable work; defer failures without reopening requests.
    restarted = FtpRequestRepository(client)
    due = await restarted.list_expiry_sync(guild, now=NOW)
    assert [row.id for row in due] == [first[0].id]
    retry_at = NOW + timedelta(minutes=10)
    await restarted.set_expiry_sync_due(due[0], retry_at)
    assert await restarted.list_expiry_sync(guild, now=NOW) == ()
    due = await restarted.list_expiry_sync(guild, now=retry_at)
    assert len(due) == 1
    await restarted.set_expiry_sync_due(due[0], None)
    # Stale retry cannot resurrect acknowledged work.
    await restarted.set_expiry_sync_due(due[0], retry_at + timedelta(minutes=10))
    assert await restarted.list_expiry_sync(guild, now=retry_at + timedelta(days=1)) == ()
    more = await restarted.expire_pending(guild, cutoff=NOW - timedelta(hours=6), now=NOW)
    assert len(more) == 2
    assert len(await restarted.list_expiry_sync(guild, now=NOW, limit=1)) == 1


async def test_concurrent_expiry_batches_do_not_duplicate_cancellations(expiry_database):
    client, repository, (guild, _) = expiry_database
    for message_id in range(1, 5):
        await insert_request(client, guild, message_id=message_id)
    batches = await asyncio.gather(*(
        repository.expire_pending(guild, cutoff=NOW - timedelta(hours=6), now=NOW, limit=2)
        for _ in range(2)
    ))
    assert all(len(batch) <= 2 for batch in batches)
    ids = [request.id for batch in batches for request in batch]
    assert len(ids) == len(set(ids)) == 4


async def test_expiry_and_manual_claim_are_atomic(expiry_database):
    client, repository, (guild, _) = expiry_database
    for message_id in range(10):
        request = await insert_request(client, guild, message_id=message_id + 1)
        expired, (claimed, changed) = await asyncio.gather(
            repository.expire_pending(guild, cutoff=NOW - timedelta(hours=6), now=NOW),
            repository.claim(request.id, actor_user_id=300),
        )
        assert len(expired) + int(changed) == 1
        final = await repository.get_by_message(guild, request.channel_id, request.message_id)
        assert final.status is (FtpRequestStatus.CLAIMED if changed else FtpRequestStatus.CANCELLED)
        assert (final.auto_cancelled_at is None) is changed
        if changed:
            assert final.claimed_by_user_id == 300


async def test_expiry_uses_utc_instants_from_timezone_conversion(expiry_database):
    from arrest_report_agent.ftp_requests.discord import parse_ftp_schedule

    client, repository, (guild, _) = expiry_database
    # Same instant from two timezones, at the exact expiry boundary.
    chile = parse_ftp_schedule("30/09/2026", "03:00", "America/Santiago")
    colombia = parse_ftp_schedule("30/09/2026", "01:00", "America/Bogota")
    assert chile == colombia == NOW - timedelta(hours=6)
    await insert_request(client, guild, message_id=1, scheduled_for=chile)
    await insert_request(client, guild, message_id=2, scheduled_for=colombia)
    assert len(await repository.expire_pending(guild, cutoff=NOW - timedelta(hours=6), now=NOW)) == 2


def test_expiry_migration_is_reversible_and_preserves_existing_requests(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires TEST_DATABASE_URL with isolated PostgreSQL")
    monkeypatch.setenv("DATABASE_URL", url)
    alembic = Config("alembic.ini")
    guild = 975432101234567812
    identifier = uuid4()

    async def inspect(*, seed=False, migrated=False, cleanup=False):
        client = PostgresClient(PostgresClientConfig(url=url))
        await client.open()
        try:
            if seed:
                await client.execute(
                    discord_guilds.insert().values(discord_id=guild, name="Expiry migration QA"),
                )
                await client.execute(sa.text(
                    "INSERT INTO ftp_training_requests "
                    "(id, guild_id, channel_id, requester_user_id, reason, status) "
                    "VALUES (:id, :guild, 50, 200, '', 'pending')"
                ), {"id": identifier, "guild": guild})
            row = await client.fetch_one(sa.text("SELECT * FROM ftp_training_requests WHERE id=:id"),
                                         {"id": identifier})
            assert row["status"] == "pending"
            assert ("auto_cancelled_at" in row) is migrated
            assert ("expiry_sync_due_at" in row) is migrated
            if migrated:
                assert row["auto_cancelled_at"] is None and row["expiry_sync_due_at"] is None
                indexes = await client.fetch_all(sa.text(
                    "SELECT indexname FROM pg_indexes WHERE tablename='ftp_training_requests'"
                ))
                assert {"ix_ftp_pending_expiry", "ix_ftp_expiry_sync"} <= {r["indexname"] for r in indexes}
        finally:
            if cleanup:
                await client.execute(discord_guilds.delete().where(discord_guilds.c.discord_id == guild))
            await client.close()

    command.upgrade(alembic, "head")
    command.downgrade(alembic, "20260929_0084")
    try:
        asyncio.run(inspect(seed=True))
        command.upgrade(alembic, "head")
        asyncio.run(inspect(migrated=True))
        command.downgrade(alembic, "20260929_0084")
        asyncio.run(inspect())
    finally:
        command.upgrade(alembic, "head")
        asyncio.run(inspect(migrated=True, cleanup=True))
