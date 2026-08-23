from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from arrest_report_agent.application.date_ranges import (
    DateTimeRangePeriod,
    resolve_compact_datetime_range,
)
from arrest_report_agent.infrastructure.sarp import (
    SarpFactionRadioLog,
    SarpFactionRadioLogPage,
)
from arrest_report_agent.knowledge import RadioCodeCatalog
from arrest_report_agent.radio_audit.agent import (
    RadioAuditRunContext,
    _sanitize_result,
)
from arrest_report_agent.radio_audit.discord import (
    _radio_log_file,
    build_radio_log_embed,
)
from arrest_report_agent.radio_audit.manual import RadioManualCatalog
from arrest_report_agent.radio_audit.models import (
    RadioAuditFindingInput,
    RadioAuditSeverity,
    RadioLogQueryResult,
    RadioLogScanTooLargeError,
)
from arrest_report_agent.radio_audit.service import RadioLogService, _parse_radio_log


def _raw(
    identifier: int,
    message: str,
    *,
    character_id: int = 10,
    character_name: str = "Derek_Valdez",
    minute: int = 0,
) -> SarpFactionRadioLog:
    return SarpFactionRadioLog.model_validate(
        {
            "id": identifier,
            "faction": 3,
            "frequency": 911,
            "character_id": character_id,
            "character_name": character_name,
            "message": message,
            "created_at": datetime(2026, 8, 22, 18, tzinfo=UTC) + timedelta(minutes=minute),
        }
    )


class FakeRadioClient:
    def __init__(self, pages: dict[int, SarpFactionRadioLogPage]) -> None:
        self.pages = pages
        self.calls: list[dict[str, object]] = []

    async def list_faction_radio_logs(
        self,
        faction_id: int,
        **kwargs: object,
    ) -> SarpFactionRadioLogPage:
        self.calls.append({"faction_id": faction_id, **kwargs})
        return self.pages[int(kwargs["page"])]


async def test_radio_service_parses_prefix_variants_and_filters_slot() -> None:
    rows = [
        _raw(1, "**[CH: SCC, S: 1]** Derek Valdez: 10-8 desde Ganton", minute=3),
        _raw(2, " [ ch : SCC , s : 12 ] D. Valdez : Radio secundario ", minute=2),
        _raw(3, "mensaje sin prefijo", minute=1),
    ]
    client = FakeRadioClient({1: SarpFactionRadioLogPage(data=rows, total=3)})

    result = await RadioLogService(client).query(  # type: ignore[arg-type]
        faction_id=3,
        date_from=datetime(2026, 8, 22, 18, tzinfo=UTC),
        date_to=datetime(2026, 8, 22, 19, tzinfo=UTC),
        slot=1,
        member_query=None,
        max_raw_records=100,
        newest_first=False,
    )

    assert [item.id for item in result.items] == [1]
    assert result.items[0].channel == "SCC"
    assert result.items[0].speaker == "Derek Valdez"
    assert result.items[0].body == "10-8 desde Ganton"
    assert result.malformed_count == 1


async def test_radio_service_deduplicates_pages_and_orders_chronologically() -> None:
    first = [
        _raw(identifier, f"[CH: SCC, S: 1] A: mensaje {identifier}", minute=identifier)
        for identifier in range(1, 501)
    ]
    second = [
        _raw(500, "[CH: SCC, S: 1] A: duplicado", minute=0),
        _raw(501, "[CH: SCC, S: 1] A: final", minute=59),
    ]
    client = FakeRadioClient(
        {
            1: SarpFactionRadioLogPage(data=first, total=501),
            2: SarpFactionRadioLogPage(data=second, total=501),
        }
    )

    result = await RadioLogService(client).query(  # type: ignore[arg-type]
        faction_id=3,
        date_from=datetime(2026, 8, 22, tzinfo=UTC),
        date_to=datetime(2026, 8, 23, 23, 59, tzinfo=UTC),
        slot=1,
        member_query="Derek",
        max_raw_records=600,
        newest_first=False,
    )

    assert len(result.items) == 501
    assert len({item.id for item in result.items}) == 501
    assert len(client.calls) == 2
    assert all(call["search"] == "Derek" for call in client.calls)
    assert result.items == tuple(sorted(result.items, key=lambda item: (item.created_at, item.id)))


async def test_radio_service_rejects_oversized_scan_before_second_page() -> None:
    client = FakeRadioClient(
        {
            1: SarpFactionRadioLogPage(
                data=[
                    _raw(identifier, f"[CH: SCC, S: 1] A: mensaje {identifier}")
                    for identifier in range(1, 501)
                ],
                total=501,
            )
        }
    )

    with pytest.raises(RadioLogScanTooLargeError) as captured:
        await RadioLogService(client).query(  # type: ignore[arg-type]
            faction_id=3,
            date_from=datetime(2026, 8, 22, 17, tzinfo=UTC),
            date_to=datetime(2026, 8, 22, 19, tzinfo=UTC),
            slot=1,
            member_query=None,
            max_raw_records=500,
            newest_first=True,
        )

    assert captured.value.total == 501
    assert len(client.calls) == 1


async def test_radio_service_member_filter_is_case_and_accent_insensitive() -> None:
    rows = [
        _raw(
            1,
            "[CH: SCC, S: 1] Álvaro Nuñez: mensaje",
            character_name="Álvaro_Nuñez",
        ),
        _raw(
            2,
            "[CH: SCC, S: 1] Terry Davis: mensaje",
            character_id=20,
            character_name="Terry_Davis",
        ),
    ]
    client = FakeRadioClient({1: SarpFactionRadioLogPage(data=rows, total=2)})

    result = await RadioLogService(client).query(  # type: ignore[arg-type]
        faction_id=3,
        date_from=datetime(2026, 8, 22, 17, tzinfo=UTC),
        date_to=datetime(2026, 8, 22, 19, tzinfo=UTC),
        slot=None,
        member_query="alvaro nunez",
        max_raw_records=100,
        newest_first=True,
    )

    assert [item.character_name for item in result.items] == ["Álvaro Nuñez"]


async def test_radio_service_filters_exact_hours_and_stops_before_older_pages() -> None:
    rows = [
        _raw(3, "[CH: SCC, S: 1] Derek Valdez: reciente", minute=120),
        _raw(2, "[CH: SCC, S: 1] Derek Valdez: incluido", minute=60),
        _raw(1, "[CH: SCC, S: 1] Derek Valdez: demasiado antiguo", minute=0),
    ]
    client = FakeRadioClient({1: SarpFactionRadioLogPage(data=rows, total=1_000)})

    result = await RadioLogService(client).query(  # type: ignore[arg-type]
        faction_id=3,
        date_from=datetime(2026, 8, 22, 18, 30, tzinfo=UTC),
        date_to=datetime(2026, 8, 22, 20, 30, tzinfo=UTC),
        slot=1,
        member_query=None,
        max_raw_records=500,
        newest_first=True,
    )

    assert [item.id for item in result.items] == [3, 2]
    assert len(client.calls) == 1
    assert client.calls[0]["date_from"] == datetime(2026, 8, 22, tzinfo=UTC).date()
    assert client.calls[0]["date_to"] == datetime(2026, 8, 22, tzinfo=UTC).date()


def test_compact_datetime_range_supports_presets_and_validates_custom_ranges() -> None:
    now = datetime(2026, 8, 23, 12, 34, 59, tzinfo=UTC)
    assert resolve_compact_datetime_range(
        DateTimeRangePeriod.LAST_2_HOURS,
        "",
        max_days=30,
        now=now,
    ) == (
        datetime(2026, 8, 23, 10, 34, 59, tzinfo=UTC),
        datetime(2026, 8, 23, 12, 34, 59, tzinfo=UTC),
    )
    assert resolve_compact_datetime_range(
        DateTimeRangePeriod.CUSTOM,
        "2026-08-01 02:30 / 2026-08-03 18:45",
        max_days=30,
        now=now,
    ) == (
        datetime(2026, 8, 1, 2, 30, tzinfo=UTC),
        datetime(2026, 8, 3, 18, 45, 59, 999_999, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="separated by slash"):
        resolve_compact_datetime_range(
            DateTimeRangePeriod.CUSTOM,
            "2026-08-01 02:30",
            max_days=30,
            now=now,
        )


def test_radio_log_embed_is_compact_zero_padded_and_hides_technical_ids() -> None:
    item = _parse_radio_log(_raw(77, "[CH: SCC, S: 1] Derek Valdez: Central, inicio patrullaje")).model_copy(
        update={"created_at": datetime(2026, 8, 23, 2, 42, tzinfo=UTC)}
    )
    result = RadioLogQueryResult(
        faction_id=3,
        date_from="2026-08-23T02:00:00+00:00",
        date_to="2026-08-23T03:00:00+00:00",
        slot=1,
        raw_total=1,
        scanned_count=1,
        malformed_count=0,
        items=(item,),
    )

    embed = build_radio_log_embed(result, result.items, page=1, page_count=1)

    assert "`02:42` · **Derek Valdez:** Central, inicio patrullaje" in (embed.description or "")
    assert "#77" not in (embed.description or "")
    assert not embed.fields


def test_radio_log_attachment_is_a_readable_one_record_per_line_txt() -> None:
    item = _parse_radio_log(_raw(77, "[CH: SCC, S: 1] Derek Valdez: Central, inicio patrullaje")).model_copy(
        update={"created_at": datetime(2026, 8, 23, 2, 42, tzinfo=UTC)}
    )
    result = RadioLogQueryResult(
        faction_id=3,
        date_from="2026-08-23T02:00:00+00:00",
        date_to="2026-08-23T03:00:00+00:00",
        slot=1,
        raw_total=1,
        scanned_count=1,
        malformed_count=0,
        items=(item,),
    )

    attachment = _radio_log_file(result)
    payload = attachment.fp.getvalue().decode("utf-8-sig")  # type: ignore[union-attr]

    assert attachment.filename == "radios-faccion-20260823-0200-slot-1.txt"
    assert "[2026-08-23 02:42 UTC] [SCC · Slot 1] Derek Valdez: Central, inicio patrullaje" in payload
    assert "#77" not in payload


def test_radio_manual_catalog_is_limited_to_communication_and_resolves_titles() -> None:
    catalog = RadioManualCatalog.load_default()

    result = catalog.search(["persecución", "parche"], limit=12)

    assert result["matched"] >= 2  # type: ignore[operator]
    assert all(
        section["key"] in {entry.key for entry in catalog.sections}
        for section in result["sections"]  # type: ignore[union-attr]
    )
    assert catalog.resolve_reference("pursuit") is not None
    assert catalog.resolve_reference("procedimiento disciplinario") is None


def test_radio_manual_catalog_lists_compact_sections_without_content() -> None:
    catalog = RadioManualCatalog.load_default()

    result = catalog.list_sections()

    assert len(result["sections"]) == len(catalog.sections)  # type: ignore[arg-type]
    assert result["sections"][0] == {  # type: ignore[index]
        "key": "format_and_callsigns",
        "title": "Códigos radiales e indicativos",
        "aliases": ["formato", "callsign", "indicativo", "estación"],
    }
    assert all("content" not in section for section in result["sections"])  # type: ignore[union-attr]


def test_radio_manual_catalog_supports_scoped_and_resilient_searches() -> None:
    catalog = RadioManualCatalog.load_default()

    scoped = catalog.search(
        ["ubicación observación"],
        section_keys=["Pursuit", "lost-observation", "does_not_exist"],
        term_match="any",
        limit=12,
    )

    assert scoped["selected_section_keys"] == ["pursuit", "lost_observation"]
    assert scoped["unknown_section_keys"] == ["does_not_exist"]
    assert {section["key"] for section in scoped["sections"]} <= {  # type: ignore[union-attr]
        "pursuit",
        "lost_observation",
    }
    assert scoped["matched"] >= 1  # type: ignore[operator]


def test_radio_manual_catalog_does_not_expand_unknown_scope_to_the_full_manual() -> None:
    catalog = RadioManualCatalog.load_default()

    result = catalog.search(
        ["persecución"],
        section_keys=["inventada"],
        term_match="all",
    )

    assert result["matched"] == 0
    assert result["sections"] == []
    assert result["unknown_section_keys"] == ["inventada"]


def test_radio_audit_sanitizes_ids_members_duplicates_and_manual_references() -> None:
    rows = (
        _parse_radio_log(_raw(1, "[CH: SCC, S: 1] Derek Valdez: mal uso")),
        _parse_radio_log(
            _raw(
                2,
                "[CH: SCC, S: 1] Terry Davis: otro mensaje",
                character_id=20,
                character_name="Terry_Davis",
            ),
        ),
    )
    context = RadioAuditRunContext(
        records=rows,
        manual=RadioManualCatalog.load_default(),
        radio_catalog=RadioCodeCatalog.load_default(),
        next_index=2,
    )
    proposed = [
        RadioAuditFindingInput(
            category="Formato",
            severity=RadioAuditSeverity.LIGHT,
            explanation="Falta información de estado.",
            manual_reference="format_and_callsigns",
            radio_ids=(1,),
        ),
        RadioAuditFindingInput(
            category="Formato",
            severity=RadioAuditSeverity.LIGHT,
            explanation="Duplicado.",
            manual_reference="format_and_callsigns",
            radio_ids=(1,),
        ),
        RadioAuditFindingInput(
            category="Mezcla",
            severity=RadioAuditSeverity.SEVERE,
            explanation="No debe unir personas.",
            manual_reference="format_and_callsigns",
            radio_ids=(1, 2),
        ),
        RadioAuditFindingInput(
            category="Inventado",
            severity=RadioAuditSeverity.SEVERE,
            explanation="Referencia inexistente.",
            manual_reference="regla inventada",
            radio_ids=(2,),
        ),
    ]

    result = _sanitize_result(context, "  Resumen breve.  ", proposed)

    assert result.summary == "Resumen breve."
    assert result.total_members == 2
    assert result.clean_members == 1
    assert result.light_count == 1
    assert result.moderate_count == 0
    assert result.severe_count == 0
    assert result.members[0].member_name == "Derek Valdez"
    assert result.members[0].findings[0].radio_ids == (1,)
