from __future__ import annotations

import pytest

from arrest_report_agent.config import PackagedPersonaProvider
from arrest_report_agent.infrastructure.discord.general_knowledge import ChargeSearchModal
from arrest_report_agent.knowledge import ChargeCatalog, RadioCodeCatalog, ReportGuide


def test_catalog_loads_expected_downloaded_dataset() -> None:
    catalog = ChargeCatalog.load_default()

    assert len(catalog.categories) == 14
    assert catalog.charge_count == 164
    assert len(catalog.source_hash) == 64


def test_catalog_search_is_accent_and_case_insensitive() -> None:
    catalog = ChargeCatalog.load_default()

    results = catalog.list_index(query="EVADIR OFICIAL", limit=10)

    assert results["matched"] == 2
    assert {item["id"] for item in results["results"]} == {115, 238}
    assert all(item["matched_fields"] == ["title"] for item in results["results"])


def test_catalog_can_search_descriptions_and_quoted_phrases() -> None:
    catalog = ChargeCatalog.load_default()

    description_result = catalog.list_index(
        query="movilización innecesaria",
        fields=["description"],
    )
    phrase_result = catalog.list_index(
        query='"orden legítima"',
        fields=["description"],
    )

    assert [item["id"] for item in description_result["results"]] == [106]
    assert description_result["results"][0]["matched_fields"] == ["description"]
    assert [item["id"] for item in phrase_result["results"]] == [262]


def test_catalog_supports_any_term_and_conservative_fuzzy_matching() -> None:
    catalog = ChargeCatalog.load_default()

    strict = catalog.list_index(query="evadir cuchillo", match_mode="all")
    broad = catalog.list_index(query="evadir cuchillo", match_mode="any")
    typo_without_fuzzy = catalog.list_index(query="evadr oficial", fuzzy=False)
    typo_with_fuzzy = catalog.list_index(query="evadr oficial", fuzzy=True)

    assert strict["matched"] == 0
    assert broad["matched"] >= 2
    assert typo_without_fuzzy["matched"] == 0
    assert {item["id"] for item in typo_with_fuzzy["results"]} == {115, 238}


def test_duplicate_codes_are_resolved_by_unique_id() -> None:
    catalog = ChargeCatalog.load_default()

    details = catalog.get_details([100, 136])

    assert [item["id"] for item in details["charges"]] == [100, 136]
    assert len({item["title"] for item in details["charges"]}) == 2


def test_charge_detail_batch_is_bounded() -> None:
    catalog = ChargeCatalog.load_default()

    with pytest.raises(ValueError, match="At most ten"):
        catalog.get_details(list(range(1, 12)))


def test_radio_catalog_loads_only_the_bounded_official_reference() -> None:
    catalog = RadioCodeCatalog.load_default()

    assert catalog.entry_count == 69
    assert len(catalog.source_hash) == 64
    assert {
        category: sum(entry.category == category for entry in catalog.entries)
        for category in ("response_code", "ten_code", "situation_code", "phonetic")
    } == {
        "response_code": 6,
        "ten_code": 13,
        "situation_code": 24,
        "phonetic": 26,
    }


def test_radio_catalog_normalizes_codes_case_accents_and_separators() -> None:
    catalog = RadioCodeCatalog.load_default()

    result = catalog.search(["211", "10 4", "ADAM", "ocean", "211-S"])
    location = catalog.search(["UBICACION ACTUAL"])

    assert {item["code"] for item in result["results"]} == {"211", "10-4", "A", "O", "211S"}
    assert result["unmatched_queries"] == []
    assert [item["code"] for item in location["results"]] == ["10-20"]


def test_radio_catalog_supports_filters_multi_query_and_conservative_fuzzy_matching() -> None:
    catalog = RadioCodeCatalog.load_default()

    without_fuzzy = catalog.search(["ambulansia"], fields=["meaning"])
    with_fuzzy = catalog.search(["ambulansia"], fields=["meaning"], fuzzy=True)
    filtered = catalog.search(
        ["homicidio", "robo vehículo"],
        categories=["situation_code"],
        fields=["meaning", "aliases"],
        term_match="all",
    )

    assert without_fuzzy["results"] == []
    assert without_fuzzy["unmatched_queries"] == ["ambulansia"]
    assert [item["code"] for item in with_fuzzy["results"]] == ["902R"]
    assert {item["code"] for item in filtered["results"]} == {"187", "215", "503"}


def test_guide_exposes_only_requested_sections() -> None:
    guide = ReportGuide.load_default()

    result = guide.get(["overview", "physical_description", "missing"])

    assert set(result["sections"]) == {"overview", "physical_description"}
    assert result["unknown_sections"] == ["missing"]
    physical = result["sections"]["physical_description"]
    assert "contradicción clara" in " ".join(physical["flexibility"])


def test_evidence_guide_treats_recordings_as_roleplay_and_requires_bag_reference() -> None:
    evidence = ReportGuide.load_default().get(["evidence"])["sections"]["evidence"]

    assert "entorno narrativo de roleplay" in evidence["role_environment"][0]
    assert "No exigir" in evidence["role_environment"][1]
    assert "número de la bolsa" in evidence["evidence_bag_reference"]["rule"]
    assert "#NÚMERO" in evidence["evidence_bag_reference"]["suggested_format"]


def test_charge_guide_filters_historical_rows_by_identity_and_proximity() -> None:
    rules = ReportGuide.load_default().get(["charge_review"])["sections"]["charge_review"]["rules"]
    rendered = " ".join(rules)

    assert "historial completo del personaje" in rendered
    assert "otro formato válido" in rendered
    assert "título del modal" in rendered
    assert "guiones bajos" in rendered
    assert "tras cruzar medianoche" in rendered
    assert "no constituye por sí sola una contradicción" in rendered
    assert "Ignorar filas claramente antiguas" in rendered


def test_review_calibration_uses_semantic_coverage_and_material_threshold() -> None:
    calibration = ReportGuide.load_default().get(["review_calibration"])["sections"]["review_calibration"]

    assert "cantidad ni un orden fijo" in calibration["semantic_coverage"][0]
    assert "cargos" in calibration["semantic_coverage"][2]
    assert "bloquea la aprobación" in calibration["blocking_threshold"][1]
    assert "agrupar errores lingüísticos" in calibration["blocking_threshold"][2]
    assert "versión más reciente" in calibration["iteration"][0]
    assert "fuente oficial" in calibration["iteration"][2]


@pytest.mark.asyncio
async def test_charge_selector_copy_comes_from_selected_persona(persona) -> None:
    roy = (await PackagedPersonaProvider().load("roy_velazco")).profile
    catalog = ChargeCatalog.load_default()
    walter_modal = ChargeSearchModal(
        catalog=catalog,
        requester_id=1,
        display_name=persona.identity.display_name,
        access_messages=persona.messages.access,
        failure_messages=persona.messages.failures,
        ui=persona.messages.interface.knowledge,
        navigation=persona.messages.interface.navigation,
    )
    roy_modal = ChargeSearchModal(
        catalog=catalog,
        requester_id=1,
        display_name=roy.identity.display_name,
        access_messages=roy.messages.access,
        failure_messages=roy.messages.failures,
        ui=roy.messages.interface.knowledge,
        navigation=roy.messages.interface.navigation,
    )

    assert walter_modal.query_input.placeholder != roy_modal.query_input.placeholder
    assert walter_modal.category_select.options[0].label == "Todas las categorías"
    assert roy_modal.category_select.options[0].label == "Todas las categorías, obvio"
    assert [child.label for child in roy_modal.children] == [
        "Qué estai buscando",
        "Dónde querís buscar",
    ]
    assert roy_modal.title == "Buscar en el código"
