from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from arrest_report_agent.application.timezones import TIMEZONE_OPTIONS, parse_local_datetime
from arrest_report_agent.config import GuildRuntimeSettings, ServiceCallOptions
from arrest_report_agent.ftp_requests.discord import FTP_TIMEZONE_OPTIONS
from arrest_report_agent.infrastructure.discord.forms import ResultVisibility
from arrest_report_agent.infrastructure.discord.service_call_statistics import (
    DiscordServiceCallStatistics,
    ServiceCallStatisticsCog,
    ServiceCallStatisticsModal,
    build_service_call_statistics_embed,
    recognize_service_call,
)
from arrest_report_agent.infrastructure.sarp import SarpEmergencyCall
from arrest_report_agent.service_calls import build_service_call_embed
from arrest_report_agent.service_calls.statistics import (
    CallCategory,
    ServiceCallStatisticsQuery,
    ServiceCallStatisticsResult,
)

NOW = datetime(2026, 10, 25, 12, tzinfo=UTC)
CALL_TIME = datetime(2026, 10, 21, 14, 30, tzinfo=UTC)
BOT_ID = 999


def test_statistics_configuration_accepts_existing_empty_payload_and_dynamic_overrides(settings):
    feature = SimpleNamespace(enabled=True, payload={})
    runtime = GuildRuntimeSettings(settings, SimpleNamespace(features={"service_calls": feature}))
    assert runtime.service_call_options == ServiceCallOptions()
    feature.payload = {"statistics_max_range_days": 14, "statistics_late_delivery_hours": 72}
    assert runtime.service_call_options.statistics_max_range_days == 14
    assert runtime.service_call_options.statistics_late_delivery_hours == 72
    feature.enabled = False
    assert runtime.service_call_options is None


@pytest.mark.parametrize("payload", (
    {"statistics_max_range_days": 0}, {"statistics_max_range_days": 366},
    {"statistics_max_messages_per_channel": 99}, {"statistics_max_messages_per_channel": 100001},
    {"statistics_late_delivery_hours": -1}, {"statistics_late_delivery_hours": 169},
    {"statistics_timeout_seconds": 0}, {"statistics_timeout_seconds": 121}, {"unknown": True},
))
def test_statistics_configuration_rejects_invalid_limits_and_unknown_keys(payload):
    with pytest.raises(ValueError):
        ServiceCallOptions.model_validate(payload)


@pytest.mark.parametrize(("zone", "local", "utc"), (
    ("America/Santiago", "01/01/2026 10:00", "2026-01-01T13:00:00+00:00"),
    ("America/Santiago", "01/07/2026 10:00", "2026-07-01T14:00:00+00:00"),
    ("Europe/Madrid", "01/01/2026 10:00", "2026-01-01T09:00:00+00:00"),
    ("Europe/Madrid", "01/07/2026 10:00", "2026-07-01T08:00:00+00:00"),
    ("America/Bogota", "01/07/2026 10:00", "2026-07-01T15:00:00+00:00"),
    ("America/Caracas", "01/07/2026 10:00", "2026-07-01T14:00:00+00:00"),
    ("America/Argentina/Buenos_Aires", "01/07/2026 10:00", "2026-07-01T13:00:00+00:00"),
    ("America/Mexico_City", "01/07/2026 10:00", "2026-07-01T16:00:00+00:00"),
    ("America/Tijuana", "01/07/2026 10:00", "2026-07-01T17:00:00+00:00"),
))
def test_timezone_conversion_uses_iana_rules_per_selected_date(zone, local, utc):
    assert parse_local_datetime(local, zone) == datetime.fromisoformat(utc)


@pytest.mark.parametrize(("value", "zone", "reason"), (
    ("29/03/2026 02:30", "Europe/Madrid", "no existe"),
    ("25/10/2026 02:30", "Europe/Madrid", "se repite"),
    ("06/09/2026 00:30", "America/Santiago", "no existe"),
    ("31/09/2026 10:00", "America/Bogota", "formato"),
    ("  ", "America/Bogota", "formato"),
    ("01/10/2026 25:00", "America/Bogota", "formato"),
    ("01/10/2026 10:00", "GMT-3", "lista"),
))
def test_timezone_validation_rejects_invalid_or_ambiguous_input(value, zone, reason):
    with pytest.raises(ValueError, match=reason):
        parse_local_datetime(value, zone)


def _query():
    return ServiceCallStatisticsQuery.parse(
        "20/10/2026 08:00", "22/10/2026 20:00", "America/Santiago", max_days=90, now=NOW,
    )


def test_range_converts_both_ends_and_includes_entire_final_minute():
    query = _query()
    assert query.starts_at == datetime(2026, 10, 20, 11, tzinfo=UTC)
    assert query.ends_at == datetime(2026, 10, 22, 23, 0, 59, 999999, tzinfo=UTC)
    assert query.includes(query.starts_at) and query.includes(query.ends_at)
    assert not query.includes(query.starts_at - timedelta(microseconds=1))
    assert not query.includes(query.ends_at + timedelta(microseconds=1))


def test_range_applies_different_offsets_across_dst_change():
    query = ServiceCallStatisticsQuery.parse(
        "05/09/2026 12:00", "07/09/2026 12:00", "America/Santiago", max_days=90, now=NOW,
    )
    assert query.starts_at.hour == 16
    assert query.ends_at.hour == 15


@pytest.mark.parametrize(("start", "end", "max_days", "reason"), (
    ("22/10/2026 12:00", "20/10/2026 12:00", 90, "posterior"),
    ("22/10/2026 12:00", "30/10/2026 12:00", 90, "futuras"),
    ("01/07/2026 12:00", "22/10/2026 12:00", 90, "máximo"),
))
def test_invalid_ranges_are_rejected(start, end, max_days, reason):
    with pytest.raises(ValueError, match=reason):
        ServiceCallStatisticsQuery.parse(start, end, "America/Santiago", max_days=max_days, now=NOW)


def test_current_minute_is_clamped_to_now():
    query = ServiceCallStatisticsQuery.parse(
        "25/10/2026 08:00", "25/10/2026 09:00", "America/Santiago", max_days=90, now=NOW,
    )
    assert query.ends_at == NOW


def _message(call_id=12, service="LSPD", *, author_id=BOT_ID, timestamp=CALL_TIME, published=None):
    call = SarpEmergencyCall(
        id=call_id, character_name="No se necesita guardar este nombre", service=service,
        situation="Información de prueba", created_at=timestamp,
    )
    return SimpleNamespace(
        author=SimpleNamespace(id=author_id), webhook_id=None,
        embeds=[build_service_call_embed(call)], created_at=published or timestamp + timedelta(seconds=10),
    )


@pytest.mark.parametrize(("service", "category"), (
    ("LSPD", CallCategory.POLICE), ("LSSD", CallCategory.POLICE),
    ("LSFD", CallCategory.FIRE_MEDICAL), ("SAMS", CallCategory.OTHER),
))
def test_recognizer_extracts_id_category_and_original_timestamp(service, category):
    assert recognize_service_call(_message(service=service), BOT_ID) == (12, category, CALL_TIME)


@pytest.mark.parametrize("invalid", ("human", "another_bot", "webhook", "title", "fields", "description"))
def test_recognizer_rejects_untrusted_messages_and_non_dispatch_embeds(invalid):
    message = _message()
    if invalid in {"human", "another_bot"}:
        message.author.id = 42
    elif invalid == "webhook":
        message.webhook_id = 888
    elif invalid == "title":
        message.embeds[0].title = "Solicitud #00012"
    elif invalid == "fields":
        message.embeds[0].clear_fields()
    else:
        message.embeds[0].description = "Texto cualquiera"
    assert recognize_service_call(message, BOT_ID) is None


def _setup(messages=(), *, role_ids=(10,), options=None):
    member = MagicMock(spec=discord.Member)
    member.id = 42
    member.roles = [SimpleNamespace(id=role_id) for role_id in role_ids]
    bot_member = MagicMock(spec=discord.Member)
    bot_member.id = BOT_ID
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 200
    channel.permissions_for.return_value = SimpleNamespace(view_channel=True, read_message_history=True)

    async def history(**kwargs):
        selected = [
            message for message in messages if kwargs["after"] < message.created_at < kwargs["before"]
        ]
        for message in selected[:kwargs["limit"]]:
            yield message

    channel.history.side_effect = history
    guild = SimpleNamespace(
        id=100, me=bot_member, get_channel=lambda channel_id: channel if channel_id == 200 else None,
        fetch_channel=AsyncMock(),
    )
    bot = SimpleNamespace(user=SimpleNamespace(id=BOT_ID))
    settings = SimpleNamespace(
        operational_staff_role_ids=(10, 20, 30, 40), service_call_channel_ids=(200,),
        service_call_options=options or ServiceCallOptions(),
    )
    provider = SimpleNamespace(cached=AsyncMock(return_value=SimpleNamespace(settings=settings)))
    return SimpleNamespace(
        member=member, bot_member=bot_member, channel=channel, guild=guild,
        reader=DiscordServiceCallStatistics(bot), settings=settings,
        cog=ServiceCallStatisticsCog(bot, provider), provider=provider,
    )


async def _count(state, *, channel_ids=(200,), options=None):
    return await state.reader.count(
        state.guild, state.member, channel_ids, _query(),
        options or state.settings.service_call_options, now=NOW,
    )


async def test_reader_counts_original_dates_deduplicates_calls_and_classifies_services():
    messages = [
        _message(1), _message(1), _message(2, "LSSD"), _message(3, "LSFD"), _message(4, "SAMS"),
        _message(5, author_id=42), _message(6, timestamp=CALL_TIME - timedelta(days=20)),
        _message(7, timestamp=CALL_TIME + timedelta(days=2)),
    ]
    state = _setup(messages)
    result = await _count(state)
    assert result.total == 4 and (result.police, result.fire_medical, result.other) == (2, 1, 1)
    assert not result.incomplete
    assert state.channel.history.call_args.kwargs["after"] == _query().starts_at - timedelta(seconds=1)
    assert state.channel.history.call_args.kwargs["before"] == (
        _query().ends_at + timedelta(hours=48, seconds=1)
    )


async def test_reader_deduplicates_between_configured_channels():
    state = _setup([_message(1)])
    other = MagicMock(spec=discord.TextChannel)
    other.id = 201
    other.permissions_for = state.channel.permissions_for
    other.history.side_effect = state.channel.history.side_effect
    channels = {200: state.channel, 201: other}
    state.guild.get_channel = channels.get
    result = await _count(state, channel_ids=(200, 201, 200))
    assert result.total == 1 and result.channel_ids == (200, 201)
    assert state.channel.history.call_count == 1


async def test_reader_includes_delayed_publications_but_filters_by_embed_time():
    query = _query()
    state = _setup([
        _message(1, published=query.ends_at + timedelta(hours=20)),
        _message(2, timestamp=query.ends_at + timedelta(minutes=1)),
        _message(3, published=query.ends_at + timedelta(hours=49)),
    ])
    result = await _count(state)
    assert result.total == 1
    assert result.late_delivery_hours == 48


async def test_reader_warns_instead_of_returning_an_exact_count_when_history_is_capped():
    options = ServiceCallOptions(statistics_max_messages_per_channel=100)
    state = _setup([_message(index + 1) for index in range(101)], options=options)
    result = await _count(state)
    assert result.total == 100 and result.truncated and result.messages_scanned == 100
    embed = build_service_call_statistics_embed(result)
    assert "Total parcial" in embed.description and "Conteo parcial" in embed.description


async def test_reader_missing_timestamp_is_not_silently_treated_as_zero():
    message = _message()
    message.embeds[0].timestamp = None
    state = _setup([message])
    result = await _count(state)
    assert result.total == 0 and result.invalid_timestamps == 1 and result.incomplete


@pytest.mark.parametrize("who", ("requester", "bot"))
async def test_reader_respects_channel_visibility_and_read_history_for_both_members(who):
    state = _setup([_message()])
    rejected_id = state.member.id if who == "requester" else BOT_ID
    state.channel.permissions_for.side_effect = lambda member: SimpleNamespace(
        view_channel=member.id != rejected_id, read_message_history=member.id != rejected_id,
    )
    with pytest.raises(ValueError, match="canales"):
        await _count(state)
    state.channel.history.assert_not_called()


async def test_reader_history_error_is_reported_as_partial():
    state = _setup()

    async def failed_history(**_kwargs):
        yield _message(1)
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "History denied")

    state.channel.history.side_effect = failed_history
    result = await _count(state)
    assert result.total == 1 and result.unavailable and result.incomplete


def _result(*, police=63, fire_medical=21, other=0):
    return ServiceCallStatisticsResult(
        query=_query(), police=police, fire_medical=fire_medical, other=other,
        channel_ids=(200,), messages_scanned=100, late_delivery_hours=48,
    )


def test_embed_contains_counts_parenthesized_percentages_selected_zone_and_local_range():
    embed = build_service_call_statistics_embed(_result())
    assert "84 llamadas únicas" in embed.description
    assert "**Policiales:** 63 (75 %)" in embed.description
    assert "**Ambulancia/bomberos:** 21 (25 %)" in embed.description
    assert "Otros" not in embed.description
    fields = {field.name: field.value for field in embed.fields}
    assert fields["Zona horaria"] == "🇨🇱 America/Santiago"
    assert fields["Desde"] == "20/10/2026 · 08:00"
    assert fields["Hasta"] == "22/10/2026 · 20:00"
    assert fields["Canales consultados"] == "<#200>"
    assert "conservadas en Discord" in embed.footer.text


def test_percentages_handle_no_calls_and_unknown_service_without_division_by_zero():
    empty = build_service_call_statistics_embed(_result(police=0, fire_medical=0))
    assert "0 llamadas únicas" in empty.description and empty.description.count("(0 %)") == 2
    mixed = build_service_call_statistics_embed(_result(police=1, fire_medical=1, other=1))
    assert "**Otros:** 1 (33,3 %)" in mixed.description


async def test_modal_reuses_ftp_timezone_choices_and_shared_public_default_visibility():
    state = _setup()
    modal = ServiceCallStatisticsModal(state.cog, 100, 42)
    assert ServiceCallStatisticsCog.service_call_statistics.name == "estadisticas-llamadas"
    assert FTP_TIMEZONE_OPTIONS is TIMEZONE_OPTIONS
    assert len(modal.children) == 4
    assert [(str(option.emoji), option.value) for option in modal.timezone.options] == list(TIMEZONE_OPTIONS)
    assert next(option.value for option in modal.visibility.options if option.default) == "public"
    assert modal.start.required and modal.end.required and modal.timezone.required


@pytest.mark.parametrize("role", (10, 20, 30, 40))
async def test_command_accepts_each_configured_operational_role(role):
    state = _setup(role_ids=(role,))
    ctx = SimpleNamespace(
        guild=state.guild, author=state.member, send_modal=AsyncMock(), respond=AsyncMock(),
    )
    await ServiceCallStatisticsCog.service_call_statistics.callback(state.cog, ctx)
    assert isinstance(ctx.send_modal.await_args.args[0], ServiceCallStatisticsModal)
    ctx.respond.assert_not_awaited()


@pytest.mark.parametrize("invalid", ("non_staff", "admin_only", "disabled", "no_channels", "dm"))
async def test_command_fails_closed_for_unauthorized_users_or_unconfigured_guilds(invalid):
    state = _setup(role_ids=() if invalid in {"non_staff", "admin_only"} else (10,))
    state.member.guild_permissions = SimpleNamespace(administrator=invalid == "admin_only")
    if invalid == "disabled":
        state.settings.service_call_options = None
    if invalid == "no_channels":
        state.settings.service_call_channel_ids = ()
    ctx = SimpleNamespace(
        guild=None if invalid == "dm" else state.guild, author=state.member,
        send_modal=AsyncMock(), respond=AsyncMock(),
    )
    await ServiceCallStatisticsCog.service_call_statistics.callback(state.cog, ctx)
    ctx.send_modal.assert_not_awaited()
    assert ctx.respond.await_args.kwargs["ephemeral"]


def _interaction(state):
    return SimpleNamespace(
        user=state.member, guild=state.guild,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


async def _submit(state, interaction, visibility=ResultVisibility.PUBLIC, **overrides):
    kwargs = {
        "guild_id": 100, "requester_id": 42, "raw_from": "01/01/2020 08:00",
        "raw_to": "02/01/2020 20:00", "timezone_key": "America/Bogota", "visibility": visibility,
    }
    await state.cog.count(interaction, **{**kwargs, **overrides})


@pytest.mark.parametrize("visibility", tuple(ResultVisibility))
async def test_full_modal_submission_reads_history_and_publishes_selected_local_range(visibility):
    timestamp = datetime(2020, 1, 1, 13, tzinfo=UTC)
    state = _setup([_message(1, timestamp=timestamp), _message(2, "LSFD", timestamp=timestamp)])
    interaction = _interaction(state)
    modal = ServiceCallStatisticsModal(state.cog, 100, 42)
    modal.start.refresh_state({"value": "01/01/2020 08:00"})
    modal.end.refresh_state({"value": "02/01/2020 20:00"})
    data = SimpleNamespace(data={})
    modal.timezone.refresh_from_modal(data, {"values": ["America/Bogota"]})
    modal.visibility.refresh_from_modal(data, {"values": [visibility.value]})
    await modal.callback(interaction)
    published = interaction.followup.send.await_args.kwargs
    assert published["ephemeral"] is (visibility is ResultVisibility.PRIVATE)
    embed = published["embed"]
    assert "2 llamadas únicas" in embed.description
    assert "**Policiales:** 1 (50 %)" in embed.description
    assert "**Ambulancia/bomberos:** 1 (50 %)" in embed.description
    fields = {field.name: field.value for field in embed.fields}
    assert fields["Zona horaria"] == "🇨🇴 America/Bogota"
    assert fields["Desde"] == "01/01/2020 · 08:00"
    assert fields["Hasta"] == "02/01/2020 · 20:00"
    state.channel.history.assert_called_once()


@pytest.mark.parametrize("visibility", tuple(ResultVisibility))
async def test_submission_defer_is_immediate_and_publishes_with_selected_visibility(visibility):
    state = _setup()
    interaction = _interaction(state)

    async def counted(*args):
        interaction.response.defer.assert_awaited_once()
        return _result()

    state.cog.reader.count = AsyncMock(side_effect=counted)
    await _submit(state, interaction, visibility)
    private = visibility is ResultVisibility.PRIVATE
    interaction.response.defer.assert_awaited_once_with(ephemeral=private)
    assert interaction.followup.send.await_args.kwargs["ephemeral"] is private
    assert interaction.followup.send.await_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    assert state.cog.reader.count.await_args.args[3].starts_at == datetime(2020, 1, 1, 13, tzinfo=UTC)
    assert not state.cog._active_guilds


@pytest.mark.parametrize("invalid", ("wrong_user", "wrong_guild", "lost_roles", "disabled", "busy"))
async def test_submission_rechecks_identity_roles_feature_and_concurrency(invalid):
    state = _setup()
    interaction = _interaction(state)
    state.cog.reader.count = AsyncMock()
    overrides = {}
    if invalid == "wrong_user":
        overrides["requester_id"] = 43
    elif invalid == "wrong_guild":
        overrides["guild_id"] = 101
    elif invalid == "lost_roles":
        state.member.roles = []
    elif invalid == "disabled":
        state.settings.service_call_options = None
    else:
        state.cog._active_guilds.add(100)
    await _submit(state, interaction, **overrides)
    state.cog.reader.count.assert_not_awaited()
    assert interaction.response.send_message.await_count + interaction.followup.send.await_count == 1
    assert (100 in state.cog._active_guilds) is (invalid == "busy")


@pytest.mark.parametrize("error", (TimeoutError(), RuntimeError("private backend details")))
async def test_submission_recovers_from_timeouts_and_unexpected_failures(error):
    state = _setup()
    interaction = _interaction(state)
    state.cog.reader.count = AsyncMock(side_effect=error)
    await _submit(state, interaction)
    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert "private backend details" not in embed.description
    assert "Total" not in embed.description
    assert not state.cog._active_guilds
