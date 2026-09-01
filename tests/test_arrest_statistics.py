from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

import discord
import pytest

from arrest_report_agent.application.ports import (
    ArrestReportCharge,
    ArrestReportDetail,
    ArrestReportDetailResult,
    ArrestReportSearchQuery,
    ArrestReportSearchResult,
    ArrestReportSummary,
)
from arrest_report_agent.arrest_statistics import (
    ArrestStatisticsCog,
    ArrestStatisticsService,
    ArrestStatisticsTooLargeError,
    aggregate_arrest_statistics,
)
from arrest_report_agent.arrest_statistics.discord import (
    ArrestStatisticsModal,
    build_arrest_statistics_embed,
)
from arrest_report_agent.config import ArrestStatisticsOptions


def _charge(
    charge_id: int,
    name: str,
    code: str,
    charge_type: str,
) -> ArrestReportCharge:
    return ArrestReportCharge(
        charge_id=charge_id,
        code=code,
        name=name,
        charge_type=charge_type,
        hours=None,
        fine=None,
    )


def _detail(report_id: int, charges: tuple[ArrestReportCharge, ...]) -> ArrestReportDetail:
    return ArrestReportDetail(
        report_id=report_id,
        suspect_name=f"Suspect {report_id}",
        officer_name="Agent Example",
        description="Yo, agente, declaro...",
        created_at=datetime(2026, 8, 31, tzinfo=UTC),
        updated_at=None,
        charges=charges,
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


def test_aggregate_counts_appearances_combinations_and_classification() -> None:
    accomplice = _charge(1, "Cómplice después del hecho", "CP-32", "MENOR")
    evasion = _charge(2, "Evadir a un oficial de la paz", "CV-2801", "NO GRAVE")
    robbery = _charge(3, "Robo mayor", "CP-487", "GRAVE")
    result = aggregate_arrest_statistics(
        (
            _detail(1, (accomplice, evasion)),
            _detail(2, (accomplice, evasion)),
            _detail(3, (robbery,)),
        ),
        date_from=date(2026, 8, 1),
        date_to=date(2026, 8, 31),
    )

    assert result.total_reports == 3
    assert result.total_charges == 5
    assert result.minor_charges == 4
    assert result.serious_charges == 1
    assert result.top_charges[0].name == "Cómplice después del hecho"
    assert result.top_charges[0].count == 2
    assert result.top_charges[0].percentage == 40
    assert result.top_combinations[0].report_count == 2
    assert result.top_combinations[0].charges == (
        ("Cómplice después del hecho", "CP-32"),
        ("Evadir a un oficial de la paz", "CV-2801"),
    )


class _Lookup:
    def __init__(self, *, total: int = 3, fail_id: int | None = None) -> None:
        self.total = total
        self.fail_id = fail_id
        self.search_calls = 0
        self.get_calls: list[int] = []

    async def search(self, query: ArrestReportSearchQuery) -> ArrestReportSearchResult:
        self.search_calls += 1
        start = (query.page - 1) * query.limit
        end = min(start + query.limit, self.total)
        reports = tuple(
            ArrestReportSummary(
                report_id=value,
                suspect_name=f"Suspect {value}",
                officer_name="Agent Example",
                description="Yo, agente...",
                created_at=datetime(2026, 8, 31, tzinfo=UTC),
            )
            for value in range(start + 1, end + 1)
        )
        return ArrestReportSearchResult(
            reports=reports,
            total=self.total,
            page=query.page,
            limit=query.limit,
            has_more=end < self.total,
        )

    async def get(self, report_id: int) -> ArrestReportDetailResult:
        self.get_calls.append(report_id)
        if report_id == self.fail_id:
            return ArrestReportDetailResult(report=None, error="temporarily_unavailable")
        return ArrestReportDetailResult(
            report=_detail(
                report_id,
                (_charge(1, "Robo mayor", "CP-487", "MENOR"),),
            )
        )

    async def bind(self, **_kwargs: object) -> str | None:
        return None

    async def get_current(self, **_kwargs: object) -> ArrestReportDetailResult:
        return ArrestReportDetailResult(report=None, error="not_bound")


@pytest.mark.asyncio
async def test_prepare_estimates_calls_and_reuses_detail_and_result_cache() -> None:
    lookup = _Lookup()
    service = ArrestStatisticsService()
    options = ArrestStatisticsOptions(faction_id=3)
    prepared = await service.prepare(
        lookup,
        options,
        date_from=date(2026, 8, 1),
        date_to=date(2026, 8, 31),
    )
    assert prepared.report_ids == (1, 2, 3)
    assert prepared.estimated_requests == 3
    result = await service.analyze(lookup, options, prepared)
    assert result.analyzed_reports == 3
    assert lookup.get_calls == [1, 2, 3]

    cached = await service.prepare(
        lookup,
        options,
        date_from=date(2026, 8, 1),
        date_to=date(2026, 8, 31),
    )
    assert cached.cached_result == result
    assert cached.estimated_requests == 0
    assert lookup.search_calls == 1
    assert await service.analyze(lookup, options, cached) == result
    assert lookup.get_calls == [1, 2, 3]


@pytest.mark.asyncio
async def test_analysis_keeps_partial_coverage_when_one_detail_fails() -> None:
    lookup = _Lookup(fail_id=2)
    service = ArrestStatisticsService()
    options = ArrestStatisticsOptions(faction_id=3)
    prepared = await service.prepare(
        lookup,
        options,
        date_from=date(2026, 8, 1),
        date_to=date(2026, 8, 31),
    )
    result = await service.analyze(lookup, options, prepared)
    assert result.total_reports == 3
    assert result.analyzed_reports == 2
    assert result.failed_reports == 1
    retry = await service.prepare(
        lookup,
        options,
        date_from=date(2026, 8, 1),
        date_to=date(2026, 8, 31),
    )
    assert retry.cached_result is None
    assert retry.cached_detail_count == 2
    assert retry.estimated_requests == 1


@pytest.mark.asyncio
async def test_prepare_stops_before_details_when_report_limit_is_exceeded() -> None:
    lookup = _Lookup(total=201)
    service = ArrestStatisticsService()
    options = ArrestStatisticsOptions(faction_id=3, max_reports=200)
    with pytest.raises(ArrestStatisticsTooLargeError) as captured:
        await service.prepare(
            lookup,
            options,
            date_from=date(2026, 8, 1),
            date_to=date(2026, 8, 31),
        )
    assert captured.value.count == 201
    assert lookup.get_calls == []


def test_embed_is_compact_and_includes_codes_and_names_in_combinations() -> None:
    result = aggregate_arrest_statistics(
        (
            _detail(
                1,
                (
                    _charge(1, "Cómplice después del hecho", "CP-32", "MENOR"),
                    _charge(2, "Evadir a un oficial de la paz", "CV-2801", "MENOR"),
                ),
            ),
        ),
        date_from=date(2026, 8, 1),
        date_to=date(2026, 8, 31),
    )
    embed = build_arrest_statistics_embed(result, top=10)
    assert "Cómplice después del hecho (CP-32)" in embed.fields[0].value
    assert "Evadir a un oficial de la paz (CV-2801)" in embed.fields[1].value
    assert len(embed.fields[0].value) <= 1_024


@pytest.mark.asyncio
async def test_modal_defaults_to_seven_days_top_ten_and_publication() -> None:
    modal = ArrestStatisticsModal(SimpleNamespace(), requester_id=42)  # type: ignore[arg-type]

    assert next(option for option in modal.period.options if option.default).value == "7"
    assert next(option for option in modal.top.options if option.default).value == "10"
    assert modal.publish.default is True
    assert [option.value for option in modal.period.options] == [
        "1",
        "2",
        "7",
        "14",
        "30",
        "custom",
    ]


@pytest.mark.asyncio
async def test_command_is_restricted_to_faction_report_roles(persona) -> None:
    denied = _member(11, (999,))
    allowed = _member(12, (200,))
    denied_responses: list[dict[str, object]] = []
    modals: list[ArrestStatisticsModal] = []

    async def cached(_guild_id: int):
        return SimpleNamespace(
            settings=SimpleNamespace(
                faction_report_role_ids=(200,),
                arrest_statistics_options=ArrestStatisticsOptions(faction_id=3),
            ),
            service=SimpleNamespace(arrest_report_lookup=object()),
            persona=SimpleNamespace(profile=persona),
        )

    async def respond(**kwargs: object) -> None:
        denied_responses.append(kwargs)

    async def send_modal(modal: ArrestStatisticsModal) -> None:
        modals.append(modal)

    cog = ArrestStatisticsCog(
        SimpleNamespace(cached=cached),  # type: ignore[arg-type]
        ArrestStatisticsService(),
        persona,
    )
    await ArrestStatisticsCog.arrest_statistics.callback(  # type: ignore[arg-type]
        cog,
        SimpleNamespace(guild=denied.guild, author=denied, respond=respond),
    )
    await ArrestStatisticsCog.arrest_statistics.callback(  # type: ignore[arg-type]
        cog,
        SimpleNamespace(guild=allowed.guild, author=allowed, send_modal=send_modal),
    )

    assert denied_responses[0]["ephemeral"] is True
    assert len(modals) == 1
