from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from arrest_report_agent.application.member_names import load_member_profiles, unique_profile_match
from arrest_report_agent.application.ports import GuildMemberProfile, GuildMemberSearchResult
from arrest_report_agent.config import FleetOptions
from arrest_report_agent.fleet.discord import FleetCog, FleetModal, FleetModelView, FleetResultView
from arrest_report_agent.fleet.rendering import fleet_embed, metric, vehicle_embed
from arrest_report_agent.fleet.service import AlprFilter, FleetService, FleetSnapshot, FleetView
from arrest_report_agent.infrastructure.sarp import SarpInvalidResponseError
from arrest_report_agent.infrastructure.sarp.models import SarpFactionVehicle, SarpFactionVehiclePage

NOW = datetime(2026, 9, 29, 12, tzinfo=UTC)
DEFAULT_OPTIONS = FleetOptions(faction_id=3)


def vehicle(identifier=1, **kwargs):
    return SarpFactionVehicle.model_validate({
        "id": identifier, "faction": 3, "name": "LSSD CVPI", "model": 598,
        "plate": str(2414802 + identifier), "visible_plate": "VISIBLE",
        "last_driver": "Charles Harrington", "health": 1183.38, "fuel": 85,
        "oil": 32, "alpr": 1, "siren": 1, "locked": 1, "impounded": 0,
        "updated_at": "2026-09-29 03:47:38", **kwargs,
    })


def page(*items, number=1, pages=1, total=None):
    return SarpFactionVehiclePage(items=list(items), total=len(items) if total is None else total,
                                 page=number, pages=pages, limit=100)


def snapshot(*items):
    return FleetSnapshot(3, tuple(items), NOW)


def profile(identifier, name):
    return GuildMemberProfile(identifier, name, f"discord{identifier}", (), None, NOW, False)


def member(identifier=11, roles=(200,)):
    catalog = {identifier: SimpleNamespace(id=identifier) for identifier in roles}
    return discord.Member(
        data={"user": {"id": str(identifier)}, "roles": [str(role) for role in roles]},
        guild=SimpleNamespace(id=1, get_role=catalog.get, default_role=None),
        state=SimpleNamespace(store_user=lambda data: SimpleNamespace(id=int(data["id"]))),
    )


def cog_and_context(*, options=DEFAULT_OPTIONS, roles=(200,), rows=None):
    settings = SimpleNamespace(operational_staff_role_ids=(200,), fleet_options=options)
    provider = SimpleNamespace(cached=AsyncMock(return_value=SimpleNamespace(settings=settings)))
    service = SimpleNamespace(snapshot=AsyncMock(return_value=snapshot(*(rows or [vehicle()]))))
    directory = SimpleNamespace(search_members=AsyncMock(return_value=GuildMemberSearchResult(
        members=(profile(11, "C. Harrington / M. Crawford"),),
    )))
    cog = FleetCog(provider, service, directory)
    author = member(roles=roles)
    interaction = SimpleNamespace(
        guild_id=1, user=author,
        response=SimpleNamespace(is_done=Mock(return_value=True), send_message=AsyncMock(),
                                 edit_message=AsyncMock(), send_modal=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    ctx = SimpleNamespace(guild=SimpleNamespace(id=1), author=author, respond=AsyncMock(),
                          defer=AsyncMock(), followup=interaction.followup, interaction=interaction)
    return cog, ctx


async def test_cache_collects_all_pages_and_coalesces_commands(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr("arrest_report_agent.fleet.service.monotonic", lambda: clock[0])
    client = SimpleNamespace(list_faction_vehicles=AsyncMock(side_effect=[
        page(vehicle(), pages=2, total=2), page(vehicle(2), number=2, pages=2, total=2),
        page(vehicle(3)),
    ]))
    service = FleetService(client)
    results = await asyncio.gather(*(service.snapshot(3) for _ in range(8)))
    assert all(result is results[0] for result in results)
    assert len(results[0].vehicles) == 2
    assert client.list_faction_vehicles.call_count == 2
    assert results[0].find_plate(" 2414804 ")[0].id == 2
    clock[0] = 129
    assert await service.snapshot(3) is results[0]
    # TTL is evaluated using the CURRENT config, not fixed at cache insertion.
    assert (await service.snapshot(3, cache_seconds=60)).vehicles[0].id == 3
    assert client.list_faction_vehicles.call_count == 3


async def test_cache_faction_isolation_and_expiry(monkeypatch):
    clock = [0]
    monkeypatch.setattr("arrest_report_agent.fleet.service.monotonic", lambda: clock[0])
    client = SimpleNamespace(list_faction_vehicles=AsyncMock(side_effect=[
        page(vehicle()), page(vehicle(2, faction=4)), page(vehicle(3)),
    ]))
    service = FleetService(client)
    first = await service.snapshot(3)
    assert (await service.snapshot(4)).vehicles[0].faction == 4
    clock[0] = 120
    assert await service.snapshot(3) is not first


@pytest.mark.parametrize("bad_page", [
    page(vehicle(faction=4)), page(vehicle(), vehicle()), page(vehicle(), total=2),
    page(vehicle(), number=2), page(vehicle(), pages=51),
])
async def test_invalid_or_partial_snapshot_is_never_cached(bad_page):
    client = SimpleNamespace(list_faction_vehicles=AsyncMock(side_effect=[bad_page, page(vehicle())]))
    service = FleetService(client)
    with pytest.raises(SarpInvalidResponseError):
        await service.snapshot(3)
    assert len((await service.snapshot(3)).vehicles) == 1


async def test_page_failure_does_not_serve_partial_or_replace_previous_snapshot(monkeypatch):
    clock = [0]
    monkeypatch.setattr("arrest_report_agent.fleet.service.monotonic", lambda: clock[0])
    client = SimpleNamespace(list_faction_vehicles=AsyncMock(side_effect=[
        page(vehicle()), page(vehicle(), total=2, pages=2), RuntimeError("offline"), page(vehicle(2)),
    ]))
    service = FleetService(client)
    await service.snapshot(3)
    clock[0] = 121
    with pytest.raises(RuntimeError, match="offline"):
        await service.snapshot(3)
    assert (await service.snapshot(3)).vehicles[0].id == 2


def test_filter_rank_lookup_and_missing_metrics():
    data = snapshot(vehicle(1, fuel=None), vehicle(2, fuel=0, alpr=0, name="Sanchez"),
                    vehicle(3, fuel=153, alpr=None))
    assert [row.id for row in data.select(FleetView.FUEL)] == [2, 3, 1]
    assert [row.id for row in data.select(FleetView.HEALTH, alpr=AlprFilter.NOT_EQUIPPED)] == [2]
    assert [row.id for row in data.select(FleetView.OIL, model="LSSD CVPI")] == [1, 3]
    assert data.select(FleetView.SUMMARY, model="missing") == ()
    assert len(data.find_plate(" visible ")) == 3  # Do not choose an arbitrary duplicate.
    assert not data.find_plate("")


@pytest.mark.parametrize("name", ["Charles_Harrington", "charles harrington", "Malcólm Crawford"])
def test_shared_name_match_resolves_full_initial_multiple_names(name):
    candidates = (profile(11, "C. Harrington / M. Crawford"), profile(12, "B. Smith"))
    resolved, ambiguous = unique_profile_match(name, candidates)
    assert resolved.user_id == 11
    assert ambiguous is False


def test_shared_match_never_guesses_on_common_surname_or_ambiguity():
    candidates = (profile(11, "C. Harrington"), profile(12, "C. Harrington / M. Crawford"))
    assert unique_profile_match("Charles Harrington", candidates) == (None, True)
    assert unique_profile_match("Harrington", candidates) == (None, False)
    full = profile(13, "Charles Harrington")
    assert unique_profile_match("Charles Harrington", (*candidates, full)) == (full, False)


async def test_member_candidates_collect_all_pages_and_propagate_incomplete_directory():
    directory = SimpleNamespace(search_members=AsyncMock(side_effect=[
        GuildMemberSearchResult((profile(1, "C. Harrington"),), next_cursor="next"),
        GuildMemberSearchResult((profile(2, "M. Crawford"),)),
    ]))
    assert len(await load_member_profiles(directory, 45, role_ids=(200,))) == 2
    assert all(call.args[0] == 45 for call in directory.search_members.call_args_list)
    assert directory.search_members.call_args.args[1].cursor == "next"
    directory.search_members.side_effect = None
    directory.search_members.return_value = GuildMemberSearchResult((), error="unavailable")
    with pytest.raises(RuntimeError):
        await load_member_profiles(directory, 45)


def test_vehicle_presentation_uses_raw_metrics_mentions_and_relative_time():
    embed = vehicle_embed(vehicle(), {"Charles Harrington": 11})
    text = str(embed.to_dict())
    assert "<@11>" in text and "1.183,38" in text
    assert "Combustible: **85**" in text and "Aceite: **32**" in text
    assert "%" not in text and "mileage" not in text and "Kilometraje" not in text
    assert ":R>" in text and "No equipada" not in text
    assert vehicle().updated_at.tzinfo == UTC
    fallback = str(vehicle_embed(vehicle(), {}).to_dict())
    assert "Charles Harrington" in fallback
    assert metric(None) == "Sin dato" and metric(0) == "0" and metric(100) == "100"


@pytest.mark.parametrize("view", list(FleetView))
def test_embeds_stay_within_discord_limits_without_omitting_rows(view):
    rows = tuple(vehicle(i, name="*" * 199 + str(i), last_driver="*" * 99) for i in range(1, 10))
    data = snapshot(*rows)
    embed = fleet_embed(data, rows, view, model=None, alpr=AlprFilter.ALL, page=0, drivers={})
    assert len(embed.description) <= 4096
    assert all(len(field.value) <= 1024 for field in embed.fields)
    assert len(embed) <= 6000
    assert embed.footer.text == "Página 1/2"
    if view is not FleetView.SUMMARY:
        assert rows[7].plate in embed.description and rows[8].plate not in embed.description


@pytest.mark.parametrize("view", list(FleetView))
def test_empty_results_are_explicit(view):
    embed = fleet_embed(snapshot(), (), view, model=None, alpr=AlprFilter.ALL, page=0, drivers={})
    assert "No hay vehículos" in embed.description


@pytest.mark.parametrize("command", ["fleet", "vehicle"])
@pytest.mark.parametrize("disabled,roles", [(False, (999,)), (True, (200,))])
async def test_commands_deny_without_operational_role_or_config(command, disabled, roles):
    cog, ctx = cog_and_context(options=None if disabled else FleetOptions(faction_id=3), roles=roles)
    args = ("2414803",) if command == "vehicle" else ()
    await getattr(FleetCog, command).callback(cog, ctx, *args)
    assert ctx.respond.call_args.kwargs["ephemeral"] is True
    cog.service.snapshot.assert_not_called()


async def test_vehicle_is_public_and_never_pings_driver():
    cog, ctx = cog_and_context()
    await FleetCog.vehicle.callback(cog, ctx, "2414803")
    ctx.defer.assert_awaited_once_with(ephemeral=False)
    sent = ctx.followup.send.call_args.kwargs
    assert "<@11>" in str(sent["embed"].to_dict())
    assert sent["allowed_mentions"].users is False
    cog.service.snapshot.assert_awaited_once_with(3, cache_seconds=120)


async def test_slash_registration_preserves_plate_option_constraints():
    cog, _ = cog_and_context()
    bot = discord.Bot(intents=discord.Intents.none())
    try:
        bot.add_cog(cog)
        commands = {command.name: command for command in cog.get_commands()}
        assert commands["flota"].options == []
        options = [option.to_dict() for option in commands["vehiculo"].options]
        assert len(options) == 1
        assert options[0]["name"] == "matricula"
        assert options[0]["description"] == "Matrícula real o visible del vehículo"
        assert options[0]["min_length"] == 1 and options[0]["max_length"] == 32
    finally:
        await bot.close()


async def test_vehicle_driver_failure_falls_back_and_api_failure_is_friendly(caplog):
    cog, ctx = cog_and_context()
    cog.directory.search_members.side_effect = RuntimeError("unavailable")
    await FleetCog.vehicle.callback(cog, ctx, "2414803")
    assert "Charles Harrington" in str(ctx.followup.send.call_args.kwargs["embed"].to_dict())
    assert "fleet_driver_matching_unavailable" in caplog.text
    cog.service.snapshot.side_effect = RuntimeError("backend failed")
    await FleetCog.fleet.callback(cog, ctx)
    assert "Inténtalo" in ctx.followup.send.call_args.kwargs["embed"].description
    assert "fleet_command_failed" in caplog.text


async def test_model_selector_covers_45_models_without_truncation_and_retains_selection():
    cog, ctx = cog_and_context(rows=[vehicle(i, name=f"Model {i:02}") for i in range(1, 46)])
    await FleetCog.fleet.callback(cog, ctx)
    ctx.defer.assert_awaited_once_with(ephemeral=True)
    panel = ctx.followup.send.call_args.kwargs["view"]
    assert panel.page_count == 2
    first_labels = {option.label for option in panel.children[0].options}
    assert len(first_labels) == 25
    panel.children[0]._selected_values = ["0"]
    panel.children[0]._interaction = SimpleNamespace(data={})
    await panel.children[0].callback(ctx.interaction)
    await panel.children[2].callback(ctx.interaction)
    second_labels = {option.label for option in panel.children[0].options}
    assert len(first_labels | second_labels) == 46
    assert panel.model == "Model 01"
    await panel.children[3].callback(ctx.interaction)
    modal = ctx.interaction.response.send_modal.call_args.args[0]
    assert modal.model == "Model 01"
    assert [o.value for o in modal.visibility.options if o.default] == ["private"]
    assert len(modal.children) == 3
    cog.service.snapshot.assert_awaited_once()


@pytest.mark.parametrize("visibility", ["private", "public"])
async def test_modal_publication_and_pagination_reuse_snapshot(visibility):
    cog, ctx = cog_and_context(rows=[vehicle(i, fuel=i) for i in range(1, 20)])
    data = await cog.service.snapshot(3)
    panel = FleetModelView(cog, 1, 11, data)
    modal = FleetModal(panel, None)
    modal.mode._selected_values = ["fuel"]
    modal.mode._interaction = SimpleNamespace(data={})
    modal.visibility._selected_values = [visibility]
    modal.visibility._interaction = SimpleNamespace(data={})
    await modal.callback(ctx.interaction)
    sent = ctx.followup.send.call_args.kwargs
    assert sent["ephemeral"] is (visibility == "private")
    assert sent["allowed_mentions"].users is False
    view = sent["view"]
    assert isinstance(view, FleetResultView)
    assert await view.interaction_check(ctx.interaction)
    await view.children[1].callback(ctx.interaction)
    assert view.page == 1 and view.embed().footer.text == "Página 2/3"
    cog.service.snapshot.assert_awaited_once()


async def test_components_revalidate_requester_guild_roles_and_faction():
    cog, ctx = cog_and_context()
    panel = FleetModelView(cog, 1, 11, snapshot(vehicle()))
    for user, guild in ((member(12), 1), (member(), 2), (member(roles=(999,)), 1)):
        ctx.interaction.user, ctx.interaction.guild_id = user, guild
        assert not await panel.interaction_check(ctx.interaction)
    ctx.interaction.user, ctx.interaction.guild_id = member(), 1
    cog.provider.cached.return_value.settings.fleet_options = FleetOptions(faction_id=4)
    assert not await panel.interaction_check(ctx.interaction)
    cog.service.snapshot.assert_not_called()


def test_fleet_config_defaults_and_validation():
    assert FleetOptions(faction_id=3).cache_seconds == 120
    for ttl in (0, -1, 3601, "120", True):
        with pytest.raises(ValueError):
            FleetOptions(faction_id=3, cache_seconds=ttl)
    with pytest.raises(ValueError):
        FleetOptions(faction_id=0)
