from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_evidence_bags import FakeEvidenceLookup, _member

from arrest_report_agent.application.ports import EvidenceBagDetail, EvidenceBagItem, EvidenceBagLookupResult
from arrest_report_agent.config import StolenWeaponOptions
from arrest_report_agent.evidence_bags import EvidenceBagCog, build_evidence_bag_embed
from arrest_report_agent.stolen_weapons.models import StolenWeaponReportLink

URL = "https://discord.com/channels/1/77/88"


def _bag(texts=("Pistola, serial: sa-001234, huellas: Persona;",)):
    return EvidenceBagDetail(
        evidence_id=10, custody=(),
        items=tuple(EvidenceBagItem(
            analysis_id=i, sample_id=i, result=text, officer_name="Analista", agency="LSSD",
            examined_at=datetime(2026, 10, 1, tzinfo=UTC),
        ) for i, text in enumerate(texts, 1)),
    )


def _setup(persona, *, texts=None, enabled=True, authorized=True, bag_error=None):
    member = _member(11, (200,) if authorized else (999,))
    options = StolenWeaponOptions(
        source_channel_ids=(42,), destination_channel_id=77, notifications_enabled=False,
    )
    bundle = SimpleNamespace(settings=SimpleNamespace(
        operational_staff_role_ids=(200,), arrest_report_lookup_faction_id=3,
        stolen_weapon_options=options if enabled else None,
    ))
    registry = SimpleNamespace(
        find_open_report_links=AsyncMock(return_value=(
            StolenWeaponReportLink("SA-001234", "ARM-A1B2C3D4E5F6", URL),
        )),
        notify=AsyncMock(), claim_alert=AsyncMock(),
    )
    detail = _bag() if texts is None else _bag(texts)
    lookup = FakeEvidenceLookup(EvidenceBagLookupResult(
        detail=detail if not bag_error else None, error=bag_error,
    ))
    ctx = SimpleNamespace(
        guild=member.guild, author=member, channel_id=555, defer=AsyncMock(), respond=AsyncMock(),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    cog = EvidenceBagCog(SimpleNamespace(cached=AsyncMock(return_value=bundle)), lookup, persona,
                         weapon_reports=registry)
    return cog, ctx, lookup, registry


async def test_command_batches_serials_and_adds_links_without_alerts_or_extra_sarp_calls(persona):
    texts = (
        "Pistola, serial: sa-001234, huellas: Persona;",
        "Rifle, número de serie: 000012;",
        "Pistola, serial: SA-001234;",
    )
    cog, ctx, lookup, registry = _setup(persona, texts=texts)
    await EvidenceBagCog.view_evidence.callback(cog, ctx, 10)
    registry.find_open_report_links.assert_awaited_once_with(1, ("SA-001234", "000012"))
    assert lookup.calls == [10]
    registry.notify.assert_not_awaited()
    registry.claim_alert.assert_not_awaited()
    published = ctx.followup.send.await_args.kwargs
    assert published["ephemeral"] is False
    assert published["allowed_mentions"].users is False and published["allowed_mentions"].roles is False
    embed = published["embed"]
    assert embed.title == "📦 Bolsa de evidencias #10"
    assert "Pistola, serial: sa-001234" in embed.fields[0].value
    assert "Cadena de custodia" in embed.fields[1].name
    assert embed.fields[2].name == "🚨 Armas reportadas como robadas"
    assert f"[Ver reporte ARM-A1B2C3D4E5F6]({URL})" in embed.fields[2].value


async def test_no_open_matches_preserves_existing_bag_format(persona):
    cog, ctx, _lookup, registry = _setup(persona)
    registry.find_open_report_links.return_value = ()
    await EvidenceBagCog.view_evidence.callback(cog, ctx, 10)
    assert len(ctx.followup.send.await_args.kwargs["embed"].fields) == 2


async def test_registry_failure_keeps_bag_and_custody_visible_without_claiming_no_theft(persona):
    cog, ctx, _lookup, registry = _setup(persona)
    registry.find_open_report_links.side_effect = RuntimeError("private-database-error")
    await EvidenceBagCog.view_evidence.callback(cog, ctx, 10)
    embed = ctx.followup.send.await_args.kwargs["embed"]
    assert "Pistola" in embed.fields[0].value and "Cadena de custodia" in embed.fields[1].name
    assert "No se pudo comprobar" in embed.fields[2].value
    assert "private-database-error" not in str(embed.to_dict())
    assert "Armas reportadas como robadas" not in str(embed.to_dict())


@pytest.mark.parametrize("case", ["disabled", "no_weapon", "missing_serial", "unauthorized", "missing_bag"])
async def test_no_cross_query_without_enabled_registry_explicit_weapon_or_permissions(persona, case):
    texts = {
        "no_weapon": ("Sangre, serial: SA-001234;",),
        "missing_serial": ("Pistola, serial: ilegible;",),
    }.get(case)
    cog, ctx, _lookup, registry = _setup(
        persona, texts=texts, enabled=case != "disabled", authorized=case != "unauthorized",
        bag_error="not_found" if case == "missing_bag" else None,
    )
    await EvidenceBagCog.view_evidence.callback(cog, ctx, 10)
    registry.find_open_report_links.assert_not_awaited()
    registry.notify.assert_not_awaited()


async def test_enabled_module_with_missing_registry_shows_degraded_check_instead_of_losing_bag(persona):
    cog, ctx, _lookup, _registry = _setup(persona)
    cog.weapon_reports = None
    await EvidenceBagCog.view_evidence.callback(cog, ctx, 10)
    assert "No se pudo comprobar" in ctx.followup.send.await_args.kwargs["embed"].fields[2].value


def test_multiple_open_reports_and_missing_publication_link_are_visible_and_bounded():
    reports = (
        StolenWeaponReportLink("SA-001234", "ARM-A1B2C3D4E5F6", URL),
        StolenWeaponReportLink("SA-001234", "ARM-B1B2C3D4E5F6", None),
    )
    embed = build_evidence_bag_embed(_bag(), stolen_weapon_reports=reports)
    field = embed.fields[2]
    assert "ARM-A1B2C3D4E5F6" in field.value and "ARM-B1B2C3D4E5F6" in field.value
    assert "enlace no disponible" in field.value and "](None)" not in field.value
    crowded = build_evidence_bag_embed(_bag(), stolen_weapon_reports=reports * 100)
    assert len(crowded.fields[2].value) <= 1024
    assert "registro(s) más" in crowded.fields[2].value
    assert len(crowded) <= 6000
