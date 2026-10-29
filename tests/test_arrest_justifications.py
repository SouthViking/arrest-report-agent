from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import discord
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from test_pending_arrest_digest import NOW, _FakeRepository, _member, _report

from arrest_report_agent.application.timezones import TIMEZONE_OPTIONS
from arrest_report_agent.config import PendingArrestReportsOptions
from arrest_report_agent.infrastructure.postgres import PostgresClient, PostgresClientConfig
from arrest_report_agent.infrastructure.postgres.schema import (
    discord_guilds,
    guild_config_revisions,
    guild_feature_settings,
    pending_arrest_justifications,
)
from arrest_report_agent.infrastructure.sarp import SarpFactionArrestSearchPayload, SarpFactionArrestSummary
from arrest_report_agent.pending_arrest_reports import (
    PendingArrestReportCog,
    PendingArrestReportService,
    build_pending_arrest_reports_embed,
)
from arrest_report_agent.pending_arrest_reports import discord as forms
from arrest_report_agent.pending_arrest_reports.digest import (
    PendingArrestDigestWorker,
    build_supervisor_alert_embed,
    due_mentions,
)
from arrest_report_agent.pending_arrest_reports.justifications import ArrestJustificationRepository
from arrest_report_agent.pending_arrest_reports.models import PendingReportJustification


def _pause(*, expires=None):
    return PendingReportJustification(
        reason="Ausencia justificada", authorized_by=99, created_at=NOW, expires_at=expires,
    )


@pytest.mark.parametrize("roles", [(), (11,), (11, 12, 13)])
def test_justification_permissions_are_independent_configurable_roles(roles):
    options = PendingArrestReportsOptions(faction_id=3, justification_role_ids=roles)
    assert options.justification_role_ids == roles


@pytest.mark.parametrize("roles", [(0,), (-1,), (11, 11), tuple(range(1, 22))])
def test_invalid_justification_roles_are_rejected(roles):
    with pytest.raises(ValueError):
        PendingArrestReportsOptions(faction_id=3, justification_role_ids=roles)


@pytest.mark.parametrize("duration,raw_until", [
    ("temporary", ""), ("temporary", "yesterday"), ("temporary", "25/09/2026 08:00"),
    ("indefinite", "27/09/2026 12:00"), ("invalid", ""),
    ("temporary", "06/09/2026 00:30"),  # Chile DST gap
])
def test_expiration_rejects_invalid_past_or_ambiguous_input(duration, raw_until):
    with pytest.raises(ValueError):
        forms.justification_expiry(duration, raw_until, "America/Santiago", now=NOW)


@pytest.mark.parametrize("timezone_key,utc_hour", [
    ("America/Argentina/Buenos_Aires", 15), ("America/Santiago", 15),
    ("America/Bogota", 17), ("America/Caracas", 16), ("Europe/Madrid", 11),
    ("Atlantic/Canary", 12), ("America/Mexico_City", 18),
    ("America/Mazatlan", 19), ("America/Hermosillo", 19),
    ("America/Cancun", 17), ("America/Tijuana", 20),
])
def test_expiration_uses_selected_timezone(timezone_key, utc_hour):
    assert forms.justification_expiry("temporary", "26/11/2026 12:00", timezone_key, now=NOW) == datetime(
        2026, 11, 26, utc_hour, tzinfo=UTC,
    )


@pytest.mark.parametrize("timezone_key", ["", "UTC", "not-a-timezone"])
def test_temporary_expiration_requires_an_explicit_supported_timezone(timezone_key):
    with pytest.raises(ValueError, match="zona horaria"):
        forms.justification_expiry("temporary", "26/11/2026 12:00", timezone_key, now=NOW)


def test_indefinite_justification_needs_neither_date_nor_timezone():
    assert forms.justification_expiry("indefinite", "", "", now=NOW) is None


@pytest.mark.parametrize("raw_until,timezone_key,now,reason", [
    ("06/09/2026 00:30", "America/Santiago", datetime(2026, 1, 1, tzinfo=UTC), "no existe"),
    ("25/10/2026 02:30", "Europe/Madrid", NOW, "se repite"),
])
def test_expiration_rejects_dst_gaps_and_ambiguities(raw_until, timezone_key, now, reason):
    with pytest.raises(ValueError, match=reason):
        forms.justification_expiry("temporary", raw_until, timezone_key, now=now)


@pytest.mark.parametrize("timezone_key", ["America/Bogota", "Europe/Madrid", ""])
async def test_modal_passes_timezone_selection_to_saving(timezone_key):
    cog = SimpleNamespace(save_justification=AsyncMock())
    modal = forms.ArrestJustificationModal(cog, requester_id=11, guild_id=1, channel_id=55, report_id=1)
    assert [(str(option.emoji), option.value) for option in modal.timezone.options] == list(TIMEZONE_OPTIONS)
    assert not any(option.default for option in modal.timezone.options)
    assert not modal.timezone.required and modal.timezone.min_values == 0
    modal.timezone.refresh_from_modal(
        SimpleNamespace(data={}), {"values": [timezone_key] if timezone_key else []},
    )
    await modal.callback(SimpleNamespace())
    assert cog.save_justification.await_args.kwargs["timezone_key"] == timezone_key


def test_suppression_is_per_report_and_expiration_restores_mentions():
    reports = (
        _report(1, age_hours=50).model_copy(update={"justification": _pause()}),
        _report(2, age_hours=50),  # same officer, a different report still causes a ping
        _report(3, age_hours=50).model_copy(update={"justification": _pause(expires=NOW)}),
    )
    assert due_mentions(reports, {1: 11, 2: 11, 3: 22}, now=NOW) == ((2, 11, 2), (3, 22, 2))
    embed = build_supervisor_alert_embed(reports, {1: 11, 2: 11, 3: 22}, summary_url="https://x", now=NOW)
    assert "#1" not in embed.description
    assert "#2" in embed.description and "#3" in embed.description


async def test_discord_payload_registers_report_id_as_integer(persona):
    cog, *_ = _command(persona)
    bot = discord.Bot(intents=discord.Intents.none())
    bot.add_cog(cog)
    group = next(cmd for cmd in bot.pending_application_commands if cmd.name == "justificar-arresto")
    group.integration_types = {discord.IntegrationType.guild_install}
    group.contexts = {discord.InteractionContextType.guild}
    payload = group.to_dict()
    assert {option["name"] for option in payload["options"]} == {"registrar", "quitar"}
    for subcommand in payload["options"]:
        option = subcommand["options"][0]
        assert option["name"] == "reporte"
        assert option["type"] == discord.SlashCommandOptionType.integer.value
        assert option["required"] and option["min_value"] == 1


@pytest.mark.parametrize("case", ["registered", "revoked", "disabled", "blocked", "bot", "no_role"])
async def test_register_and_remove_require_configured_current_roles(monkeypatch, persona, case):
    cog, ctx, _bundle, repository = _command(persona)
    if case == "revoked":
        ctx.guild.fetch_member.return_value.roles = []
    elif case == "disabled":
        _bundle.settings.pending_arrest_reports_options = None
    elif case == "blocked":
        _bundle.settings.agent_blocked_user_allowlist.ids = (11,)
    elif case == "bot":
        ctx.author.bot = True
        ctx.guild.fetch_member.return_value.bot = True
    elif case == "no_role":
        ctx.author.roles = []
        ctx.guild.fetch_member.return_value.roles = []
    if case != "revoked":
        await PendingArrestReportCog.register_justification.callback(cog, ctx, 1)
        assert ctx.send_modal.await_count == (1 if case == "registered" else 0)
    await PendingArrestReportCog.remove_justification.callback(cog, ctx, 1)
    assert repository.remove.await_count == (1 if case == "registered" else 0)
    repository.register.assert_not_awaited()


def _command(persona):
    member = MagicMock(spec=discord.Member)
    member.id, member.bot = 11, False
    member.roles = [SimpleNamespace(id=200)]
    fresh = MagicMock(spec=discord.Member)
    fresh.id, fresh.bot = 11, False
    fresh.roles = [SimpleNamespace(id=200)]
    guild = SimpleNamespace(id=1, fetch_member=AsyncMock(return_value=fresh))
    ctx = SimpleNamespace(
        guild=guild, channel=SimpleNamespace(id=55), author=member,
        respond=AsyncMock(), defer=AsyncMock(), send_modal=AsyncMock(),
        interaction=SimpleNamespace(id=100),
    )
    bundle = SimpleNamespace(settings=SimpleNamespace(
        pending_arrest_reports_options=PendingArrestReportsOptions(
            faction_id=3, justification_role_ids=(200,),
        ),
        agent_blocked_user_allowlist=SimpleNamespace(ids=()),
        report_channel_allowlist=SimpleNamespace(ids=(7,)),
        supervisor_role_allowlist=SimpleNamespace(ids=(8,)),
    ))
    repository = SimpleNamespace(register=AsyncMock(), remove=AsyncMock(return_value=True))
    service = SimpleNamespace(
        client=SimpleNamespace(get_arrest=AsyncMock(return_value=SimpleNamespace(created_at=NOW))),
        scan=AsyncMock(return_value=_scan((_report(1, age_hours=50),))),
    )
    return PendingArrestReportCog(SimpleNamespace(cached=AsyncMock(return_value=bundle)),
                                 service, persona, repository), ctx, bundle, repository


def _scan(reports):
    from arrest_report_agent.pending_arrest_reports import PendingArrestReportScan
    return PendingArrestReportScan(
        faction_id=3, date_from=NOW.date(), date_to=NOW.date(), total_reports=len(reports),
        pending_reports=reports,
    )


@pytest.mark.parametrize("case", [
    "temporary", "indefinite", "revoked", "changed_config", "wrong_user", "wrong_guild", "wrong_channel",
    "not_pending", "sarp_failure", "audit_failure", "changed_after_scan", "empty_reason",
    "missing_timezone", "invalid_timezone",
])
async def test_modal_rechecks_identity_permissions_sarp_and_audit(monkeypatch, persona, case):
    cog, ctx, bundle, repository = _command(persona)
    monkeypatch.setattr(forms.discord, "ApplicationContext", lambda *_: ctx)
    modal = forms.ArrestJustificationModal(cog, requester_id=11, guild_id=1, channel_id=55, report_id=1)
    assert modal.reason.style == discord.InputTextStyle.long and modal.reason.required
    assert {option.value for option in modal.duration.options} == {"temporary", "indefinite"}
    assert not modal.until.required
    interaction = SimpleNamespace(
        id=101, user=ctx.author, guild=ctx.guild, channel=ctx.channel, client=SimpleNamespace(),
        response=SimpleNamespace(send_message=AsyncMock()),
    )
    if case == "wrong_user":
        interaction.user = SimpleNamespace(id=99)
    elif case == "wrong_guild":
        interaction.guild = SimpleNamespace(id=99)
    elif case == "wrong_channel":
        interaction.channel = SimpleNamespace(id=99)
    elif case == "revoked":
        ctx.guild.fetch_member.return_value.roles = []
    elif case == "changed_config":
        bundle.settings.pending_arrest_reports_options = PendingArrestReportsOptions(faction_id=3)
    elif case == "not_pending":
        cog.service.scan.return_value = _scan(())
    elif case == "sarp_failure":
        cog.service.client.get_arrest.side_effect = RuntimeError("SARP unavailable")
    elif case == "audit_failure":
        repository.register.side_effect = RuntimeError("Database unavailable")
    elif case == "changed_after_scan":
        async def revoke(**kwargs):
            ctx.guild.fetch_member.return_value.roles = []
            return _scan((_report(1, age_hours=50),))
        cog.service.scan.side_effect = revoke
    temporary = case in {"temporary", "missing_timezone", "invalid_timezone"}
    await cog.save_justification(
        interaction, requester_id=11, guild_id=1, channel_id=55, report_id=1,
        reason=" " if case == "empty_reason" else "Ausencia justificada",
        duration="temporary" if temporary else "indefinite",
        raw_until="01/01/2099 12:00" if temporary else "",
        timezone_key="invalid" if case == "invalid_timezone" else "" if case == "missing_timezone"
            else "America/Bogota",
    )
    if case in {"temporary", "indefinite"}:
        saved = repository.register.await_args.kwargs
        assert saved["report_id"] == 1 and saved["authorized_by"] == 11 and saved["interaction_id"] == 101
        assert (saved["expires_at"] is not None) == (case == "temporary")
        if case == "temporary":
            assert saved["expires_at"] == datetime(2099, 1, 1, 17, tzinfo=UTC)
            assert "Hasta 01/01/2099 12:00 · 🇨🇴 America/Bogota" in ctx.respond.await_args.args[0]
        assert ctx.guild.fetch_member.await_count == 2
        assert cog.service.scan.await_args.kwargs["faction_id"] == 3
        ctx.defer.assert_awaited_once_with(ephemeral=True, invisible=False)
    elif case == "audit_failure":
        assert "No se pudo confirmar" in ctx.respond.await_args.args[0]
    else:
        repository.register.assert_not_awaited()
    if case.startswith("wrong_"):
        interaction.response.send_message.assert_awaited_once()
        ctx.defer.assert_not_awaited()


@pytest.mark.parametrize("all_excused", [False, True])
async def test_worker_pauses_only_selected_reports_and_preserves_pending_summary(persona, all_excused):
    reports = (_report(1, age_hours=50).model_copy(update={"justification": _pause()}),)
    if not all_excused:
        reports += (_report(2, age_hours=50, officer="Richard Castellanos"),)
    result = _scan(reports)
    embed = build_pending_arrest_reports_embed(result, ui=persona.messages.interface.pending_arrest_reports)
    assert f"{len(reports)} de {len(reports)} pendientes" in embed.description
    assert "1 con justificación" in embed.description
    assert "sin menciones" in embed.description and "Sin vencimiento" in embed.description
    options = PendingArrestReportsOptions(
        faction_id=3, digest_targets=({"channel_id": 55, "mention_users": True},),
        supervisor_alerts_enabled=True, supervisor_alert_channel_ids=(66,), supervisor_alert_role_id=8,
    )
    bundle = SimpleNamespace(
        snapshot=SimpleNamespace(guild_id=1), persona=SimpleNamespace(profile=persona),
        settings=SimpleNamespace(operational_staff_role_ids=(100,)),
    )
    message = SimpleNamespace(id=1001, reply=AsyncMock())
    channel = SimpleNamespace(send=AsyncMock(return_value=message))
    role = SimpleNamespace(id=8, mention="<@&8>", mentionable=True)
    worker = PendingArrestDigestWorker(
        SimpleNamespace(get_guild=lambda _: SimpleNamespace(id=1, emojis=[], get_role=lambda _: role)),
        SimpleNamespace(),
        _FakeRepository(), SimpleNamespace(), SimpleNamespace(),
    )
    worker._channel = AsyncMock(return_value=channel)
    worker._members = AsyncMock(return_value=(
        _member(11, "Malcolm Crawford"), _member(22, "Richard Castellanos"),
    ))
    await worker._publish(bundle, options, result, cycle_id=uuid4(), now=NOW)
    if all_excused:
        assert worker._channel.await_count == 1  # no Supervisory alert even though overdue
        assert channel.send.await_args.kwargs["allowed_mentions"].users is False
        message.reply.assert_not_awaited()
    else:
        assert worker._channel.await_count == 2
        assert "<@22>" in message.reply.await_args.args[0]
        assert "<@11>" not in message.reply.await_args.args[0]
        alert = channel.send.await_args.kwargs
        assert alert["content"] == "<@&8>"
        assert "#2" in alert["embed"].description and "#1" not in alert["embed"].description


async def test_service_annotates_without_changing_compliance_or_adding_sarp_calls():
    summary = SarpFactionArrestSummary(
        id=1, police_id=11, police_name="Officer One", suspect_id=12,
        suspect_name="Suspect One", description="Pendiente", created_at=NOW,
    )
    client = SimpleNamespace(search_faction_arrests=AsyncMock(return_value=SarpFactionArrestSearchPayload(
        data=[summary], total=1,
    )))
    repository = SimpleNamespace(for_scan=AsyncMock(return_value={1: _pause()}))
    service = PendingArrestReportService(client, justifications=repository)
    result = await service.scan(faction_id=3, date_from=NOW.date(), date_to=NOW.date(), now=NOW, guild_id=1)
    assert result.pending_count == 1 and result.completed_count == 0 and result.compliance_percentage == 0
    assert result.pending_reports[0].justification == _pause()
    assert client.search_faction_arrests.await_count == 1
    assert repository.for_scan.await_args.kwargs["observed_ids"] == (1,)


async def test_migration_and_durable_lifecycle_are_scoped_and_preserve_history(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires isolated PostgreSQL")
    monkeypatch.setenv("DATABASE_URL", url)
    alembic = Config("alembic.ini")
    await asyncio.to_thread(command.downgrade, alembic, "base")
    await asyncio.to_thread(command.upgrade, alembic, "20261026_0095")
    client = PostgresClient(PostgresClientConfig(url=url))
    await client.open()
    revisions = {}
    originals = {}
    try:
        async with client.transaction() as tx:
            for guild in (1234674521416663070, 999):
                await tx.execute(discord_guilds.insert().values(discord_id=guild))
                for number, (status, custom) in enumerate((
                    ("draft", False), ("published", False), ("draft", True), ("archived", False),
                ), 1):
                    rev = uuid4()
                    revisions[(guild, status, custom)] = rev
                    payload = {"faction_id": 3, "digest_interval_hours": 12}
                    if custom:
                        payload["justification_role_ids"] = []
                    originals[rev] = payload
                    await tx.execute(guild_config_revisions.insert().values(
                        id=rev, guild_id=guild, revision=number, status=status,
                    ))
                    await tx.execute(guild_feature_settings.insert().values(
                        config_revision_id=rev, feature_key="pending_arrest_reports",
                        payload=payload, enabled=True,
                    ))
        await asyncio.to_thread(command.upgrade, alembic, "head")
        rows = await client.fetch_all(sa.select(guild_feature_settings).where(
            guild_feature_settings.c.feature_key == "pending_arrest_reports",
        ))
        for row in rows:
            key = next(key for key, rev in revisions.items() if rev == row["config_revision_id"])
            guild, status, custom = key
            expected = dict(originals[row["config_revision_id"]])
            if guild == 1234674521416663070 and status != "archived" and not custom:
                expected["justification_role_ids"] = [
                    1234676131240869949, 1234678029662294047, 1234678238182244414,
                ]
            assert row["payload"] == expected
        repo = ArrestJustificationRepository(client)
        base = dict(guild_id=999, faction_id=3, report_id=1, officer_name="Officer One",
                    reason="Ausencia justificada", authorized_by=11, now=NOW, expires_at=None)
        await repo.register(**base, interaction_id=101)
        await repo.register(**base, interaction_id=101)  # same interaction is idempotent
        query = dict(guild_id=999, faction_id=3, observed_ids=(1,), pending_ids=(1,), now=NOW)
        assert 1 in await repo.for_scan(**query)
        assert await repo.for_scan(**{**query, "faction_id": 4}) == {}
        assert await repo.for_scan(**{**query, "guild_id": 1234674521416663070}) == {}
        await repo.remove(guild_id=999, faction_id=4, report_id=1, user_id=11, interaction_id=102, now=NOW)
        assert 1 in await repo.for_scan(**query)
        await repo.register(**{**base, "expires_at": NOW + timedelta(hours=2)}, interaction_id=103)
        # Simulate a restart with another repository instance.
        repo = ArrestJustificationRepository(client)
        assert 1 in await repo.for_scan(**query)
        assert await repo.for_scan(**{**query, "now": NOW + timedelta(hours=2)}) == {}
        await repo.register(**base, interaction_id=104)
        # An absent report is not proof of completion (lookback window / pagination).
        assert await repo.for_scan(**{**query, "observed_ids": (), "pending_ids": ()}) == {}
        assert 1 in await repo.for_scan(**query)
        await repo.for_scan(**{**query, "pending_ids": ()})
        assert await repo.for_scan(**query) == {}  # completed does not resurrect the old pause
        await repo.register(**base, interaction_id=105)
        assert await repo.remove(guild_id=999, faction_id=3, report_id=1, user_id=12,
                                 interaction_id=106, now=NOW)
        await repo.register(**base, interaction_id=107)
        # A retried old removal must not delete a newer authorization.
        assert await repo.remove(guild_id=999, faction_id=3, report_id=1, user_id=12,
                                 interaction_id=106, now=NOW)
        assert 1 in await repo.for_scan(**query)
        history = await client.fetch_all(sa.select(pending_arrest_justifications).order_by(
            pending_arrest_justifications.c.interaction_id,
        ))
        assert [r["end_reason"] for r in history] == ["replaced", "expired", "completed", "removed", None]
        assert history[-2]["ended_by"] == 12
        await asyncio.gather(
            *(repo.register(**{**base, "report_id": 2}, interaction_id=i) for i in (110, 111))
        )
        concurrent = await client.fetch_all(sa.select(pending_arrest_justifications).where(
            pending_arrest_justifications.c.report_id == 2,
        ))
        assert len(concurrent) == 2
        assert sum(row["ended_at"] is None for row in concurrent) == 1
        await asyncio.to_thread(command.downgrade, alembic, "20261026_0095")
    finally:
        await client.close()
        await asyncio.to_thread(command.downgrade, alembic, "base")
