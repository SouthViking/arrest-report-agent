"""Deterministic tool, notification and closure tests around the existing Pycord/SDK test patterns."""

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from agents.tool_context import ToolContext
from test_evidence_bags import context
from test_stolen_weapons import DESTINATION, GUILD, NOW, SOURCE, USER, _details, _file, _interaction, _setup

from arrest_report_agent.application.ports import (
    EvidenceBagDetail,
    EvidenceBagItem,
    EvidenceBagLookupResult,
    WeaponTheftSerialResult,
)
from arrest_report_agent.application.tools import build_agent_tools
from arrest_report_agent.domain import AgentAccessLevel, ReviewPublication, ReviewVerdict
from arrest_report_agent.domain.weapon_serials import extract_weapon_serials
from arrest_report_agent.infrastructure.discord.publisher import build_review_embeds
from arrest_report_agent.infrastructure.discord.stolen_weapons import StolenWeaponCog, TheftClosureModal
from arrest_report_agent.infrastructure.discord.weapon_theft_alerts import WeaponTheftAlertNotifier
from arrest_report_agent.knowledge import ChargeCatalog, ReportGuide
from arrest_report_agent.stolen_weapons.lookup import WeaponTheftLookupService


@pytest.mark.parametrize(
    "text, serials",
    [
        ("[Balística: 5250444] Bersa Firestorm, serial: 71380487, huellas: Persona;", ("71380487",)),
        ("Escopeta, número de serie: SA-00012;", ("SA-00012",)),
        ("Pistola, serial: 00012; pistola, serial: 00012;", ("00012",)),
        ("Pistola, serial: abc_123/4;", ("ABC_123/4",)),
        ("Pistola, serial: ilegible;", ()),
        ("Pistola, serial: no definido;", ()),
        ("Pistola, serial: n/a;", ()),
        ("Pistola, serial: <@123>;", ()),
        ("[Balística: 5250444] Pistola con huellas: Persona;", ()),
        ("Teléfono, serial: 1234567;", ()),
        ("Bolsa 5303358; muestra 5250444.", ()),
    ],
)
def test_extracts_only_explicit_serials_of_weapons(text, serials):
    assert extract_weapon_serials(text) == serials


async def _report(state, **changes):
    report = await state.service.register(
        submission_id=1001,
        guild_id=GUILD,
        source_channel_id=SOURCE,
        destination_channel_id=DESTINATION,
        registered_by=USER,
        details=_details(),
        files=(_file(),),
        max_file_bytes=100000,
        max_total_bytes=500000,
    )
    return replace(report, message_id=555, publication_status="published", **changes)


async def test_registry_batch_returns_open_closed_missing_and_invalid_serials():
    state = _setup()
    first = await _report(state)
    closed = replace(
        first,
        id=uuid4(),
        status="closed",
        closed_at=NOW,
        closed_by=USER,
        closure_reason="Arma localizada",
        closure_interaction_id=123,
    )
    other = replace(closed, details=_details(serial="00012"))
    repo = SimpleNamespace(find_by_serials=AsyncMock(return_value=(first, closed, other)))
    service = WeaponTheftLookupService(repo)
    results = await service.lookup(GUILD, ("sa-001234", "00012", "9999", "<@123>"))
    assert [result.status for result in results] == [
        "reported_stolen",
        "closed",
        "no_records",
        "invalid_serial",
    ]
    assert len(results[0].reports) == 2
    assert {report["status"] for report in results[0].reports} == {"open", "closed"}
    assert results[1].reports[0]["closure_reason"] == "Arma localizada"
    assert "message_url" not in results[0].reports[0]
    repo.find_by_serials.assert_awaited_once_with(GUILD, ("SA-001234", "00012", "9999"))
    repo.find_by_serials.side_effect = RuntimeError("unavailable")
    assert (await service.lookup(GUILD, ("00012",)))[0].status == "unavailable"


async def _invoke(tool, ctx, payload):
    arguments = json.dumps(payload)
    return await tool.on_invoke_tool(
        ToolContext(
            context=ctx,
            tool_name=tool.name,
            tool_call_id=f"call-{tool.name}",
            tool_arguments=arguments,
        ),
        arguments,
    )


def _registry_tools(*, count=2, status="reported_stolen", access=AgentAccessLevel.REPORT_OWNER):
    ctx = context(access_level=access)
    ctx.case = ctx.case.model_copy(update={"structured_report_id": 751})
    ctx.publisher = SimpleNamespace(mark_review_started=AsyncMock(), publish=AsyncMock())
    serials = ("71380487", "00012345")[:count]
    ids = (5303358, 5303359)[:count]
    results = {
        bag: EvidenceBagLookupResult(
            detail=EvidenceBagDetail(
                evidence_id=bag,
                custody=(),
                items=(
                    EvidenceBagItem(
                        analysis_id=1,
                        sample_id=2,
                        result=f"[Balística: 2] Pistola, serial: {serial}, huellas: Persona;",
                        officer_name="Analista",
                        agency="LSSD",
                        examined_at=NOW,
                    ),
                ),
            )
        )
        for bag, serial in zip(ids, serials, strict=True)
    }
    bags = SimpleNamespace(get=AsyncMock(side_effect=lambda id: results[id]))

    async def lookup(_guild, requested):
        return tuple(
            WeaponTheftSerialResult(
                serial,
                status,
                (
                    {
                        "reference": "ARM-A1B2C3D4E5F6",
                        "status": "closed" if status == "closed" else "open",
                        "description": "Datos internos",
                        "victim_name": "Víctima privada",
                        "evidence_count": 2,
                    },
                ),
            )
            for serial in requested
        )

    theft = SimpleNamespace(lookup=AsyncMock(side_effect=lookup), notify=AsyncMock())
    tools = {
        tool.name: tool
        for tool in build_agent_tools(
            ChargeCatalog.load_default(),
            ReportGuide.load_default(),
            access_level=access,
            evidence_bag_lookup=bags,
            weapon_theft_lookup=theft,
            report_channel_ids=frozenset({ctx.case.parent_channel_id}),
        )
    }
    return ctx, tools, bags, theft, serials, ids


@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("access", [AgentAccessLevel.REPORT_OWNER, AgentAccessLevel.FULL])
async def test_batch_tools_reuse_lookup_notify_once_and_publish_basic_nonblocking_information(count, access):
    ctx, tools, bags, theft, serials, ids = _registry_tools(count=count, access=access)
    data = await _invoke(tools["get_evidence_bag"], ctx, {"bags": [{"evidence_id": bag} for bag in ids]})
    assert [result["items"][0]["weapon_serials"][0] for result in data["results"]] == list(serials)
    theft.lookup.assert_awaited_once_with(ctx.case.guild_id, serials)
    theft.notify.assert_awaited_once()
    found = await _invoke(tools["get_weapon_theft_reports"], ctx, {"serials": list(serials)})
    assert found["affects_approval"] is False
    assert len(found["results"]) == count
    assert theft.lookup.await_count == theft.notify.await_count == 1
    if access is AgentAccessLevel.REPORT_OWNER:
        assert "Víctima privada" not in str(found) and "Datos internos" not in str(found)
    publication = ReviewPublication(verdict=ReviewVerdict.APPROVED, summary="Reporte aprobado.")
    await _invoke(
        tools["execute_arrest_report_review"], ctx, {"publication": publication.model_dump(mode="json")}
    )
    published = ctx.publisher.publish.await_args.args[0]
    assert published.verdict is ReviewVerdict.APPROVED and published.findings == []
    assert len(published.informational_notes) == count
    assert all(serial in published.model_dump_json() for serial in serials)
    assert "ARM-" not in published.model_dump_json() and "https://" not in published.model_dump_json()


@pytest.mark.parametrize("status", ["closed", "no_records", "unavailable"])
async def test_inactive_and_unavailable_results_do_not_notify_or_add_an_active_notice(status):
    ctx, tools, _bags, theft, serials, ids = _registry_tools(status=status)
    await _invoke(tools["get_evidence_bag"], ctx, {"bags": [{"evidence_id": bag} for bag in ids]})
    result = await _invoke(tools["get_weapon_theft_reports"], ctx, {"serials": list(serials)})
    assert all(item["status"] == status for item in result["results"])
    theft.notify.assert_not_awaited()
    publication = ReviewPublication(verdict=ReviewVerdict.APPROVED, summary="Reporte aprobado.")
    await _invoke(
        tools["execute_arrest_report_review"], ctx, {"publication": publication.model_dump(mode="json")}
    )
    assert ctx.publication.informational_notes == [] and ctx.publication.verdict is ReviewVerdict.APPROVED


async def test_notification_failure_does_not_block_the_review():
    ctx, tools, _bags, theft, _serials, ids = _registry_tools()
    theft.notify.side_effect = RuntimeError("Discord unavailable")
    await _invoke(tools["get_evidence_bag"], ctx, {"bags": [{"evidence_id": bag} for bag in ids]})
    publication = ReviewPublication(verdict=ReviewVerdict.APPROVED, summary="Reporte aprobado.")
    await _invoke(
        tools["execute_arrest_report_review"], ctx, {"publication": publication.model_dump(mode="json")}
    )
    assert ctx.publication.verdict is ReviewVerdict.APPROVED


async def test_owner_cannot_query_an_unverified_serial():
    ctx, tools, _bags, theft, _serials, _ids = _registry_tools()
    result = await _invoke(tools["get_weapon_theft_reports"], ctx, {"serials": ["99999"]})
    assert result["error"] == "serial_not_verified_in_report"
    theft.lookup.assert_not_awaited()


async def test_generic_serial_lookup_without_a_report_bag_does_not_send_notifications():
    ctx, tools, _bags, theft, serials, _ids = _registry_tools(access=AgentAccessLevel.FULL)
    await _invoke(tools["get_weapon_theft_reports"], ctx, {"serials": list(serials)})
    theft.lookup.assert_awaited_once()
    theft.notify.assert_not_awaited()


async def test_closure_command_opens_a_modal_without_writing_and_rechecks_roles():
    state = _setup()
    ctx = SimpleNamespace(
        guild=state.guild,
        channel=state.source,
        author=state.member,
        send_modal=AsyncMock(),
        respond=AsyncMock(),
    )
    await StolenWeaponCog.close_theft.callback(state.cog, ctx)
    modal = ctx.send_modal.await_args.args[0]
    assert isinstance(modal, TheftClosureModal)
    assert modal.title == "Cerrar reporte de robo"
    state.member.roles = []
    await modal.callback(_interaction(state))
    state.repository.create.assert_not_awaited()
    assert ctx.respond.await_count == 0


async def test_closure_updates_original_card_and_preserves_evidence_attachments():
    state = _setup()
    report = await _report(state)
    closed = replace(
        report,
        status="closed",
        closed_at=NOW,
        closed_by=USER,
        closure_reason="Arma localizada",
        closure_interaction_id=100,
    )
    state.repository.get_by_reference = AsyncMock(return_value=report)
    state.repository.close = AsyncMock(return_value=(closed, True))
    message = SimpleNamespace(edit=AsyncMock())
    state.destination.fetch_message = AsyncMock(return_value=message)
    modal = TheftClosureModal(state.cog, state.context)
    modal.reference.refresh_state({"value": report.reference})
    modal.reason.refresh_state({"value": "Arma localizada"})
    interaction = _interaction(state)
    await modal.callback(interaction)
    state.repository.close.assert_awaited_once_with(
        GUILD, report.id, user_id=USER, interaction_id=100, reason="Arma localizada"
    )
    update = message.edit.await_args.kwargs
    assert "files" not in update and "attachments" not in update
    assert update["embeds"][0].title == "✅ Reporte de robo cerrado"
    assert update["embeds"][0].image.url == f"attachment://{report.assets[0].filename}"
    assert any(field.name == "Motivo del cierre" for field in update["embeds"][0].fields)
    interaction.delete_original_response.assert_awaited_once()


async def test_closure_database_state_survives_discord_edit_failure():
    state = _setup()
    report = await _report(state)
    closed = replace(
        report,
        status="closed",
        closed_at=NOW,
        closed_by=USER,
        closure_reason="Arma localizada",
        closure_interaction_id=100,
    )
    state.repository.get_by_reference = AsyncMock(return_value=report)
    state.repository.close = AsyncMock(return_value=(closed, True))
    state.destination.fetch_message = AsyncMock(side_effect=RuntimeError("Discord offline"))
    modal = TheftClosureModal(state.cog, state.context)
    modal.reference.refresh_state({"value": report.reference})
    modal.reason.refresh_state({"value": "Arma localizada"})
    interaction = _interaction(state)
    await modal.callback(interaction)
    assert "quedó cerrado" in interaction.edit_original_response.await_args.kwargs["content"]


async def test_alert_uses_only_configured_channel_with_links_and_no_mentions():
    state = _setup()
    report = await _report(state)
    state.settings.report_channel_allowlist = SimpleNamespace(ids=frozenset({SOURCE}))
    state.repository.find_by_serials = AsyncMock(return_value=(report,))
    state.repository.claim_alert = AsyncMock(return_value=True)
    state.repository.mark_alert = AsyncMock()
    bot = SimpleNamespace(get_guild=lambda _id: state.guild)
    notifier = WeaponTheftAlertNotifier(bot, state.cog.provider, state.repository)
    case = context().case.model_copy(update={"guild_id": GUILD, "parent_channel_id": SOURCE})
    await notifier.notify(case, {5303358: (report.details.normalized_serial,)})
    sent = state.destination.send.await_args.kwargs
    data = str(sent["embed"].to_dict())
    assert report.jump_url in data
    assert str(case.root_message_id) in data and "5303358" in data
    assert sent["allowed_mentions"].users is False and sent["allowed_mentions"].roles is False
    state.repository.mark_alert.assert_awaited_once_with(
        GUILD, report.id, case.root_message_id, status="sent", message_id=555
    )
    state.repository.claim_alert.return_value = False
    await notifier.notify(case, {5303358: (report.details.normalized_serial,)})
    assert state.destination.send.await_count == 1


@pytest.mark.parametrize("closed,enabled", [(True, True), (False, False)])
async def test_closed_records_and_disabled_notifications_do_not_alert(closed, enabled):
    state = _setup()
    report = await _report(state, status="closed" if closed else "open")
    state.settings.report_channel_allowlist = SimpleNamespace(ids=frozenset({SOURCE}))
    state.settings.stolen_weapon_options = state.settings.stolen_weapon_options.model_copy(
        update={"notifications_enabled": enabled},
    )
    state.repository.find_by_serials = AsyncMock(return_value=(report,))
    notifier = WeaponTheftAlertNotifier(
        SimpleNamespace(get_guild=lambda _: state.guild), state.cog.provider, state.repository
    )
    case = context().case.model_copy(update={"guild_id": GUILD, "parent_channel_id": SOURCE})
    await notifier.notify(case, {5303358: (report.details.normalized_serial,)})
    state.destination.send.assert_not_awaited()


def test_information_is_rendered_separately_and_does_not_change_verdict(persona):
    publication = ReviewPublication(
        verdict=ReviewVerdict.APPROVED,
        summary="Reporte aprobado.",
        informational_notes=["Serial 71380487 en bolsa 5303358: robo abierto."],
    )
    embeds = build_review_embeds(publication, persona)
    assert publication.findings == [] and publication.verdict is ReviewVerdict.APPROVED
    assert any("71380487" in str(embed.to_dict()) for embed in embeds)
