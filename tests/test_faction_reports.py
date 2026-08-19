from __future__ import annotations

import io
import zipfile
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import discord
import pytest

from arrest_report_agent.application.date_ranges import DateRangePeriod, resolve_date_range
from arrest_report_agent.faction_directory import (
    FactionActivityLevel,
    FactionDirectoryService,
    FactionDirectorySnapshot,
    activity_level_for,
)
from arrest_report_agent.faction_directory.models import FactionDirectoryMember
from arrest_report_agent.faction_reports import (
    FactionReport,
    FactionReportCog,
    FactionReportMember,
    FactionReportModal,
    FactionReportService,
    FactionReportTooLargeError,
    FactionReportVisibility,
    build_faction_report_workbook,
)
from arrest_report_agent.faction_reports.discord import _ready_embed
from arrest_report_agent.infrastructure.sarp import (
    SarpFactionBankEntry,
    SarpFactionBankPage,
    SarpFactionDetail,
    SarpFactionRank,
)


def _ranks() -> list[SarpFactionRank]:
    return [
        SarpFactionRank(id=1, faction=3, name="Deputy Sheriff", paycheck=500),
        SarpFactionRank(id=2, faction=3, name="Sergeant", paycheck=800),
        SarpFactionRank(id=3, faction=3, name="Lieutenant", paycheck=1_000),
    ]


def _detail(now: datetime) -> SarpFactionDetail:
    return SarpFactionDetail.model_validate(
        {
            "factionMembers": [
                {
                    "id": 1,
                    "name": "Zoe_Adams",
                    "online": 1,
                    "factionduty": 0,
                    "rank": 2,
                    "lastcon": (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
                    "factiontime": 600,
                },
                {
                    "id": 2,
                    "name": "Álvaro_Beltran",
                    "online": 0,
                    "factionduty": 0,
                    "rank": 3,
                    "lastcon": (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S"),
                    "factiontime": 1_200,
                },
                {
                    "id": 3,
                    "name": "Ana_Mendez",
                    "online": 0,
                    "factionduty": 0,
                    "rank": 2,
                    "lastcon": (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"),
                    "factiontime": 300,
                },
            ]
        }
    )


def _bank_entry(entry_id: int) -> SarpFactionBankEntry:
    return SarpFactionBankEntry.model_validate(
        {
            "id": entry_id,
            "faction_id": 3,
            "character_id": 100 + entry_id,
            "character_name": f"Member_{entry_id}",
            "concept": f"Movimiento {entry_id}",
            "money": -100 * entry_id,
            "created_at": f"2026-08-19 0{entry_id}:00:00",
        }
    )


def _member(user_id: int, role_ids: tuple[int, ...]) -> discord.Member:
    roles = {role_id: SimpleNamespace(id=role_id) for role_id in role_ids}
    guild = SimpleNamespace(id=1, get_role=roles.get, default_role=None)
    state = SimpleNamespace(store_user=lambda data: SimpleNamespace(id=int(data["id"])))
    return discord.Member(
        data={"user": {"id": str(user_id)}, "roles": [str(role_id) for role_id in role_ids]},
        guild=guild,
        state=state,
    )


async def test_shared_directory_maps_one_based_ranks_and_sorts_hierarchy_then_name() -> None:
    now = datetime(2026, 8, 19, 12, tzinfo=UTC)
    calls = {"detail": 0, "ranks": 0}

    async def detail(_faction_id: int) -> SarpFactionDetail:
        calls["detail"] += 1
        return _detail(now)

    async def ranks(_faction_id: int) -> list[SarpFactionRank]:
        calls["ranks"] += 1
        return _ranks()

    directory = FactionDirectoryService(
        SimpleNamespace(get_faction_detail=detail, get_faction_ranks=ranks),
        roster_cache_seconds=30,
        rank_cache_seconds=300,
    )
    first = await directory.snapshot(3)
    second = await directory.snapshot(3)

    assert [(member.name, member.rank_name, member.salary) for member in first.members] == [
        ("Álvaro Beltran", "Lieutenant", 1_000),
        ("Ana Mendez", "Sergeant", 800),
        ("Zoe Adams", "Sergeant", 800),
    ]
    assert second == first
    assert calls == {"detail": 1, "ranks": 1}


def test_activity_classifier_uses_stable_pcu_bands() -> None:
    now = datetime(2026, 8, 19, 12, tzinfo=UTC)

    assert activity_level_for(now - timedelta(days=3), now=now) is FactionActivityLevel.VERY_ACTIVE
    assert activity_level_for(now - timedelta(days=7), now=now) is FactionActivityLevel.ACTIVE
    assert activity_level_for(now - timedelta(days=14), now=now) is FactionActivityLevel.REGULAR
    assert activity_level_for(now - timedelta(days=21), now=now) is FactionActivityLevel.AT_RISK
    assert activity_level_for(now - timedelta(days=22), now=now) is FactionActivityLevel.INACTIVE
    assert activity_level_for(None, now=now) is FactionActivityLevel.UNKNOWN


def test_shared_date_ranges_cover_quick_presets_and_custom_bounds() -> None:
    today = date(2026, 8, 19)

    assert resolve_date_range(DateRangePeriod.LAST_1_DAY, "", "", max_days=30, today=today) == (
        today,
        today,
    )
    assert resolve_date_range(DateRangePeriod.LAST_2_DAYS, "", "", max_days=30, today=today) == (
        date(2026, 8, 18),
        today,
    )
    assert resolve_date_range(
        DateRangePeriod.CUSTOM,
        "2026-08-01",
        "2026-08-19",
        max_days=30,
        today=today,
    ) == (date(2026, 8, 1), today)
    with pytest.raises(ValueError):
        resolve_date_range(
            DateRangePeriod.CUSTOM,
            "2026-01-01",
            "2026-08-19",
            max_days=30,
            today=today,
        )


async def test_report_service_paginates_caches_and_filters_active_members() -> None:
    now = datetime(2026, 8, 19, 12, tzinfo=UTC)
    directory = SimpleNamespace(
        snapshot=lambda _faction_id: _async_value(
            FactionDirectorySnapshot(
                faction_id=3,
                members=(
                    FactionDirectoryMember(
                        "Captain Example",
                        False,
                        False,
                        "Captain",
                        9,
                        1_500,
                        now - timedelta(days=2),
                        900,
                    ),
                    FactionDirectoryMember(
                        "Inactive Example",
                        False,
                        False,
                        "Deputy Sheriff",
                        2,
                        500,
                        now - timedelta(days=40),
                        300,
                    ),
                ),
            )
        )
    )
    calls: list[int] = []

    async def bank(_faction_id: int, **kwargs: object) -> SarpFactionBankPage:
        page = int(kwargs["page"])
        calls.append(page)
        entries = [_bank_entry(1)] if page == 1 else [_bank_entry(2)]
        return SarpFactionBankPage(data=entries, total=2)

    service = FactionReportService(
        SimpleNamespace(list_faction_bank_logs=bank),
        directory,
        bank_cache_seconds=60,
    )
    first = await service.generate(
        faction_id=3,
        date_from=date(2026, 8, 18),
        date_to=date(2026, 8, 19),
        now=now,
    )
    second = await service.generate(
        faction_id=3,
        date_from=date(2026, 8, 18),
        date_to=date(2026, 8, 19),
        now=now,
    )

    assert calls == [1, 2]
    assert [entry.id for entry in first.bank_entries] == [2, 1]
    assert [member.name for member in first.active_members] == ["Captain Example"]
    assert second.bank_entries == first.bank_entries


async def test_report_service_rejects_large_bank_ranges_after_first_page() -> None:
    async def bank(_faction_id: int, **_kwargs: object) -> SarpFactionBankPage:
        return SarpFactionBankPage(data=[], total=10_001)

    service = FactionReportService(
        SimpleNamespace(list_faction_bank_logs=bank),
        SimpleNamespace(snapshot=lambda _faction_id: _async_value(FactionDirectorySnapshot(3, ()))),
    )
    with pytest.raises(FactionReportTooLargeError):
        await service.generate(
            faction_id=3,
            date_from=date(2026, 8, 1),
            date_to=date(2026, 8, 19),
            max_bank_records=10_000,
        )


def test_workbook_contains_only_minimal_columns_and_sorted_members() -> None:
    report = FactionReport(
        faction_id=3,
        date_from=date(2026, 8, 18),
        date_to=date(2026, 8, 19),
        generated_at=datetime(2026, 8, 19, 12, tzinfo=UTC),
        bank_entries=(_bank_entry(1),),
        active_members=(
            FactionReportMember(
                "Chief Example",
                "Chief",
                10,
                2_000,
                FactionActivityLevel.VERY_ACTIVE,
                600,
            ),
        ),
        total_members=1,
    )

    content = build_faction_report_workbook(report, organization_name="Sheriff Department")

    with zipfile.ZipFile(io.BytesIO(content)) as workbook:
        bank_xml = workbook.read("xl/worksheets/sheet1.xml").decode()
        members_xml = workbook.read("xl/worksheets/sheet2.xml").decode()
        assert "Fecha (UTC)" in bank_xml
        assert "Responsable" in bank_xml
        assert "Concepto" in bank_xml
        assert "Monto" in bank_xml
        assert "character_id" not in bank_xml
        assert "Miembros activos" in members_xml
        assert "Horas de servicio" in members_xml
        assert "lastcon" not in members_xml
        assert "Chief Example" in members_xml
        assert bank_xml.index("<autoFilter") < bank_xml.index("<mergeCells")
        assert members_xml.index("<autoFilter") < members_xml.index("<mergeCells")


def test_workbook_sanitizes_xml_control_characters() -> None:
    entry = _bank_entry(1).model_copy(update={"concept": "Depósito\x00corregido"})
    report = FactionReport(
        faction_id=3,
        date_from=date(2026, 8, 19),
        date_to=date(2026, 8, 19),
        generated_at=datetime(2026, 8, 19, 12, tzinfo=UTC),
        bank_entries=(entry,),
        active_members=(),
        total_members=0,
    )

    content = build_faction_report_workbook(report, organization_name="Sheriff Department")

    with zipfile.ZipFile(io.BytesIO(content)) as workbook:
        bank_xml = workbook.read("xl/worksheets/sheet1.xml").decode()
    assert "\x00" not in bank_xml
    assert "Depósito�corregido" in bank_xml


def test_ready_embed_does_not_repeat_selected_visibility(persona) -> None:
    report = FactionReport(
        faction_id=3,
        date_from=date(2026, 8, 19),
        date_to=date(2026, 8, 19),
        generated_at=datetime(2026, 8, 19, 12, tzinfo=UTC),
        bank_entries=(),
        active_members=(),
        total_members=0,
    )

    embed = _ready_embed(report, persona.messages.interface.faction_reports)

    assert embed.fields == []


async def test_command_uses_dedicated_role_policy_and_opens_private_modal(persona) -> None:
    denied_responses: list[dict[str, object]] = []
    allowed_modals: list[FactionReportModal] = []
    denied = _member(11, (999,))
    allowed = _member(12, (200,))

    async def cached(_guild_id: int):
        return SimpleNamespace(
            settings=SimpleNamespace(
                faction_report_role_ids=(200,),
                faction_report_options=SimpleNamespace(faction_id=3),
            ),
            persona=SimpleNamespace(profile=persona),
        )

    async def respond(**kwargs: object) -> None:
        denied_responses.append(kwargs)

    async def send_modal(modal: FactionReportModal) -> None:
        allowed_modals.append(modal)

    cog = FactionReportCog(SimpleNamespace(cached=cached), SimpleNamespace(), persona)
    await FactionReportCog.faction_report.callback(  # type: ignore[arg-type]
        cog,
        SimpleNamespace(guild=denied.guild, author=denied, respond=respond),
    )
    await FactionReportCog.faction_report.callback(  # type: ignore[arg-type]
        cog,
        SimpleNamespace(guild=allowed.guild, author=allowed, send_modal=send_modal),
    )

    assert denied_responses[0]["ephemeral"] is True
    assert denied_responses[0]["embed"].description == (
        persona.messages.interface.faction_reports.unauthorized
    )
    assert len(allowed_modals) == 1
    assert [option.value for option in allowed_modals[0].period.options] == [
        "1",
        "2",
        "7",
        "14",
        "30",
        "90",
        "custom",
    ]
    assert allowed_modals[0].publish.default is False


async def test_modal_checkbox_maps_to_private_and_public_visibility() -> None:
    captured: list[FactionReportVisibility] = []

    async def generate(_interaction: object, **kwargs: object) -> None:
        captured.append(FactionReportVisibility(str(kwargs["visibility"])))

    modal = FactionReportModal(SimpleNamespace(generate=generate), requester_id=10)  # type: ignore[arg-type]
    await modal.callback(SimpleNamespace())  # type: ignore[arg-type]
    modal.publish.refresh_state({"value": True})
    await modal.callback(SimpleNamespace())  # type: ignore[arg-type]

    assert captured == [FactionReportVisibility.PRIVATE, FactionReportVisibility.PUBLIC]


async def _async_value(value):
    return value
