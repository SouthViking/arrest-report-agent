from __future__ import annotations

import pytest

from arrest_report_agent.knowledge import ChargeCatalog, ReportGuide


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


def test_guide_exposes_only_requested_sections() -> None:
    guide = ReportGuide.load_default()

    result = guide.get(["overview", "physical_description", "missing"])

    assert set(result["sections"]) == {"overview", "physical_description"}
    assert result["unknown_sections"] == ["missing"]
    physical = result["sections"]["physical_description"]
    assert "contradicción clara" in " ".join(physical["flexibility"])
