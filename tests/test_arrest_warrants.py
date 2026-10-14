from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError

from arrest_report_agent.application.ports import ArrestReportDetail
from arrest_report_agent.arrest_warrants.repository import ArrestWarrantAlertRepository
from arrest_report_agent.arrest_warrants.service import ArrestWarrantMonitor
from arrest_report_agent.config import ArrestReportLookupOptions
from arrest_report_agent.domain import FindingSeverity, ReviewFinding, ReviewIssueType, ReviewPublication
from arrest_report_agent.infrastructure.postgres import PostgresClient, PostgresClientConfig
from arrest_report_agent.infrastructure.postgres.schema import discord_guilds
from arrest_report_agent.infrastructure.sarp import SarpWarrant, SarpWarrantDetail, SarpWarrantPage
from arrest_report_agent.structured_arrest_reports.discord import StructuredArrestReportCog

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
REPORT = ArrestReportDetail(321, "Andy Quintero", "Charles Harrington", "Yo, ...", NOW, None, (), 44)
OPTIONS = ArrestReportLookupOptions(
    faction_id=3,
    warrant_alerts_enabled=True,
    warrant_alert_channel_ids=(66,),
    warrant_alert_role_id=20,
)


def _warrant(warrant_id=7, **overrides):
    return SarpWarrant.model_validate(
        {
            "id": warrant_id,
            "suspect_id": 44,
            "type": ["arrest"],
            "status": 1,
            "created_at": "2026-10-12 12:00:00",
            "description": "Fundamento",
            **overrides,
        }
    )


def _detail(warrant_id=7, **overrides):
    return SarpWarrantDetail.model_validate(
        {
            **_warrant(warrant_id).model_dump(by_alias=True),
            "suspect_name": "Andy_Quintero",
            "filed_name": "Dennis_Irvan",
            "signed_name": "Jane_Judge",
            "signed_at": "2026-10-13 12:00:00",
            **overrides,
        }
    )


def _client(items=(), details=None):
    return SimpleNamespace(
        list_active_warrants=AsyncMock(
            return_value=SarpWarrantPage(
                items=list(items),
                total=len(items),
                page=1,
                pages=1,
                limit=100,
            )
        ),
        get_warrant=AsyncMock(side_effect=lambda warrant_id: (details or {7: _detail()})[warrant_id]),
    )


class MemoryDeliveries:
    def __init__(self):
        self.delivered = set()
        self.lock = asyncio.Lock()

    async def deliver_once(self, *, guild_id, report_id, channel_id, warrant_ids, send):
        async with self.lock:
            pending = tuple(
                i for i in warrant_ids if (guild_id, report_id, channel_id, i) not in self.delivered
            )
            if not pending:
                return False
            await send(pending)
            self.delivered.update((guild_id, report_id, channel_id, i) for i in pending)
            return True


def _monitor(client, *, repository=None):
    role = SimpleNamespace(id=20, mention="<@&20>", mentionable=True)
    guild = SimpleNamespace(id=1, me=SimpleNamespace(), get_role=lambda i: role if i == 20 else None)
    channel = Mock(spec=discord.TextChannel)
    channel.guild = guild
    channel.send = AsyncMock(return_value=SimpleNamespace(id=555))
    guild.get_channel = lambda i: channel if i == 66 else None
    bot = SimpleNamespace(get_guild=lambda i: guild if i == 1 else None)
    return ArrestWarrantMonitor(bot, client, repository or MemoryDeliveries()), channel


def _payload(context):
    return json.loads(context.split("\n", 1)[1])


@pytest.mark.asyncio
async def test_no_open_warrants_costs_one_call_and_sends_no_alert():
    client = _client()
    monitor, channel = _monitor(client)
    context = await monitor.check_and_notify(
        guild_id=1, report=REPORT, report_url="https://discord.test/1", options=OPTIONS
    )
    client.list_active_warrants.assert_awaited_once_with(44, page=1, limit=100)
    client.get_warrant.assert_not_awaited()
    channel.send.assert_not_awaited()
    assert _payload(context)["status"] == "confirmed"
    assert _payload(context)["open_arrest_warrants"] == []


@pytest.mark.asyncio
async def test_first_submission_groups_orders_and_reuses_issuer_details_without_duplicate_pings():
    client = _client([_warrant(7), _warrant(8)], {7: _detail(7), 8: _detail(8, type=["arrest", "search"])})
    monitor, channel = _monitor(client)
    contexts = await asyncio.gather(
        *(
            monitor.check_and_notify(
                guild_id=1,
                report=REPORT,
                report_url="https://discord.test/1",
                options=OPTIONS,
            )
            for _ in range(2)
        )
    )
    channel.send.assert_awaited_once()
    sent = channel.send.await_args.kwargs
    assert sent["content"] == "<@&20>"
    assert sent["enforce_nonce"] is True
    assert sent["allowed_mentions"].users is False
    assert sent["allowed_mentions"].everyone is False
    assert [r.id for r in sent["allowed_mentions"].roles] == [20]
    embed_text = str(sent["embed"].to_dict())
    assert "Dennis Irvan" in embed_text and "Jane Judge" in embed_text
    assert "https://discord.test/1" in embed_text
    assert len(sent["embed"].fields) == 2
    payload = _payload(contexts[0])
    assert len(payload["open_arrest_warrants"]) == 2
    assert payload["open_arrest_warrants"][0]["filed_name"] == "Dennis Irvan"
    assert payload["open_arrest_warrants"][0]["predates_arrest"] is True
    assert payload["open_arrest_warrants"][0]["detail_available"] is True


@pytest.mark.asyncio
async def test_search_only_closed_and_wrong_character_orders_are_not_notified():
    client = _client(
        [
            _warrant(7, type=["search"]),
            _warrant(8, closed_at=NOW),
            _warrant(9, suspect_id=99),
            _warrant(10),
        ],
        {10: _detail(10, closed_at=NOW)},
    )
    monitor, channel = _monitor(client)
    check = await monitor.inspect(REPORT)
    assert check.warrants == ()
    client.get_warrant.assert_awaited_once_with(10)
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_detail_identity_is_checked_before_exposing_any_data():
    client = _client([_warrant()], {7: _detail(suspect_id=99)})
    monitor, _ = _monitor(client)
    check = await monitor.inspect(REPORT)
    assert check.status == "incomplete"
    assert check.warrants == ()


@pytest.mark.asyncio
async def test_pending_signature_and_new_order_are_distinguished_from_historical_validity():
    client = _client(
        [_warrant()],
        {7: _detail(signed_at=None, signed_name="Firstname_Lastname", created_at="2026-10-14 13:00:00")},
    )
    monitor, channel = _monitor(client)
    context = await monitor.check_and_notify(
        guild_id=1, report=REPORT, report_url="https://discord.test/1", options=OPTIONS
    )
    assert "Pendiente de firma" in str(channel.send.await_args.kwargs["embed"].to_dict())
    order = _payload(context)["open_arrest_warrants"][0]
    assert order["signed_name"] is None
    assert order["predates_arrest"] is False


@pytest.mark.asyncio
async def test_partial_detail_still_alerts_without_inventing_issuer_or_signature():
    client = _client([_warrant()])
    client.get_warrant.side_effect = RuntimeError("SARP unavailable")
    monitor, channel = _monitor(client)
    context = await monitor.check_and_notify(
        guild_id=1, report=REPORT, report_url="https://discord.test/1", options=OPTIONS
    )
    assert _payload(context)["status"] == "incomplete"
    assert _payload(context)["open_arrest_warrants"][0]["detail_available"] is False
    text = str(channel.send.await_args.kwargs["embed"].to_dict())
    assert "Firma no disponible" in text and "No informado" in text
    assert "Pendiente de firma" not in text


@pytest.mark.asyncio
async def test_list_failure_is_unavailable_not_confirmed_absence():
    client = _client()
    client.list_active_warrants.side_effect = RuntimeError("SARP unavailable")
    monitor, channel = _monitor(client)
    context = await monitor.check_and_notify(
        guild_id=1, report=REPORT, report_url="https://discord.test/1", options=OPTIONS
    )
    assert _payload(context)["status"] == "unavailable"
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_notifications_keep_agent_context_and_failed_delivery_does_not_lose_context():
    monitor, channel = _monitor(_client([_warrant()]))
    context = await monitor.check_and_notify(
        guild_id=1,
        report=REPORT,
        report_url="https://discord.test/1",
        options=OPTIONS.model_copy(update={"warrant_alerts_enabled": False}),
    )
    channel.send.assert_not_awaited()
    assert _payload(context)["supervisor_notification"] == "disabled"
    channel.send.side_effect = RuntimeError("Discord unavailable")
    context = await monitor.check_and_notify(
        guild_id=1, report=REPORT, report_url="https://discord.test/1", options=OPTIONS
    )
    assert _payload(context)["supervisor_notification"] == "incomplete"
    assert len(_payload(context)["open_arrest_warrants"]) == 1


@pytest.mark.asyncio
async def test_pagination_and_grouped_embed_limits_are_bounded():
    items = [_warrant(i, description="a" * 10000) for i in range(1, 12)]
    client = _client(
        items, {i: _detail(i, filed_name="a" * 1000, signed_name="b" * 1000) for i in range(1, 12)}
    )
    client.list_active_warrants.side_effect = [
        SarpWarrantPage(items=items[:5], total=11, page=1, limit=100, pages=2),
        SarpWarrantPage(items=items[5:], total=11, page=2, limit=100, pages=2),
    ]
    monitor, channel = _monitor(client)
    context = await monitor.check_and_notify(
        guild_id=1, report=REPORT, report_url="https://discord.test/1", options=OPTIONS
    )
    assert client.list_active_warrants.await_count == 2
    assert client.get_warrant.await_count == 10
    assert _payload(context)["status"] == "incomplete"
    embed = channel.send.await_args.kwargs["embed"]
    assert len(embed) <= 6000
    assert len(embed.fields) == 10


@pytest.mark.asyncio
async def test_initial_command_hook_runs_independently_of_model_and_survives_administrative_failure():
    review = SimpleNamespace(run_structured_initial_review=AsyncMock())
    monitor = SimpleNamespace(check_and_notify=AsyncMock(return_value="warrant snapshot"))
    provider = SimpleNamespace(
        cached=AsyncMock(
            return_value=SimpleNamespace(
                settings=SimpleNamespace(arrest_report_lookup_options=OPTIONS),
            )
        )
    )
    cog = StructuredArrestReportCog(provider, SimpleNamespace(), review, monitor)
    args = dict(
        root=SimpleNamespace(jump_url="https://discord.test/1"),
        thread=SimpleNamespace(),
        requester=SimpleNamespace(),
        submission=SimpleNamespace(guild_id=1),
        arrest_report=REPORT,
        report_snapshot_current=True,
    )
    await cog._run_initial_review(**args)
    assert (
        review.run_structured_initial_review.await_args.kwargs["arrest_warrant_context"] == "warrant snapshot"
    )
    monitor.check_and_notify.side_effect = RuntimeError("admin subsystem unavailable")
    await cog._run_initial_review(**args)
    assert review.run_structured_initial_review.await_count == 2
    assert review.run_structured_initial_review.await_args.kwargs["arrest_warrant_context"] is None


def test_warrant_finding_cannot_be_a_required_correction():
    finding = dict(
        issue_type=ReviewIssueType.OPEN_ARREST_WARRANT,
        title="Orden abierta",
        detail="La orden sigue abierta.",
        recommendation="Verifica si corresponde cerrarla.",
    )
    with pytest.raises(ValidationError, match="non-blocking"):
        ReviewFinding(**finding, severity=FindingSeverity.REQUIRED)
    assert (
        ReviewFinding(**finding, severity=FindingSeverity.OBSERVATION).severity is FindingSeverity.OBSERVATION
    )
    publication = ReviewPublication(
        verdict="approved",
        summary="Reporte aprobado.",
        findings=[ReviewFinding(**finding, severity=FindingSeverity.OBSERVATION)],
    )
    assert publication.verdict.value == "approved"


@pytest.mark.parametrize(
    "overrides",
    [
        {"warrant_alerts_enabled": True},
        {"warrant_alert_channel_ids": [-1]},
        {"warrant_alert_channel_ids": [66, 66]},
        {"warrant_alert_role_id": 0},
    ],
)
def test_alert_configuration_rejects_missing_or_invalid_destinations(overrides):
    with pytest.raises(ValidationError):
        ArrestReportLookupOptions(faction_id=3, **overrides)


@pytest.mark.asyncio
async def test_missing_suspect_id_does_not_query_another_character():
    client = _client()
    monitor, _ = _monitor(client)
    check = await monitor.inspect(replace(REPORT, suspect_id=None))
    assert check.status == "unavailable"
    client.list_active_warrants.assert_not_awaited()


@pytest.mark.asyncio
async def test_database_delivery_deduplicates_concurrency_and_preserves_failed_send_for_retry(monkeypatch):
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("requires isolated PostgreSQL")
    monkeypatch.setenv("DATABASE_URL", database_url)
    alembic = Config("alembic.ini")
    await asyncio.to_thread(command.upgrade, alembic, "head")
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    guild_id = 8887766
    try:
        await client.execute(discord_guilds.insert().values(discord_id=guild_id))
        repository = ArrestWarrantAlertRepository(client)
        send = AsyncMock(return_value=555)
        args = dict(guild_id=guild_id, report_id=321, channel_id=66, warrant_ids=(7, 8), send=send)
        results = await asyncio.gather(*(repository.deliver_once(**args) for _ in range(2)))
        assert sorted(results) == [False, True]
        send.assert_awaited_once_with((7, 8))
        failing = AsyncMock(side_effect=RuntimeError("Discord unavailable"))
        with pytest.raises(RuntimeError):
            await repository.deliver_once(**{**args, "warrant_ids": (9,), "send": failing})
        assert await repository.deliver_once(**{**args, "warrant_ids": (9,)})
        assert send.await_args.args == ((9,),)
    finally:
        await client.execute(discord_guilds.delete().where(discord_guilds.c.discord_id == guild_id))
        await client.close()
