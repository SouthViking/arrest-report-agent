"""Bounded, cached aggregation of SARP arrest charges."""

from __future__ import annotations

import asyncio
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import date
from time import monotonic

from arrest_report_agent.application.ports import (
    ArrestReportCharge,
    ArrestReportDetail,
    ArrestReportLookup,
    ArrestReportSearchQuery,
)
from arrest_report_agent.config import ArrestStatisticsOptions

from .models import (
    ArrestStatisticsResult,
    ArrestStatisticsTooLargeError,
    ArrestStatisticsUnavailableError,
    ChargeCombinationStatistic,
    ChargeStatistic,
    PreparedArrestStatistics,
)

PAGE_SIZE = 50


@dataclass(slots=True)
class _TimedValue[T]:
    value: T
    stored_at: float


class ArrestStatisticsService:
    """Prepare report IDs cheaply, then aggregate details only after confirmation."""

    def __init__(self) -> None:
        self._summary_cache: dict[
            tuple[int, date, date],
            _TimedValue[tuple[int, ...]],
        ] = {}
        self._detail_cache: dict[
            tuple[int, int],
            _TimedValue[ArrestReportDetail],
        ] = {}
        self._result_cache: dict[
            tuple[int, date, date],
            _TimedValue[ArrestStatisticsResult],
        ] = {}
        self._cache_lock = asyncio.Lock()

    async def prepare(
        self,
        lookup: ArrestReportLookup,
        options: ArrestStatisticsOptions,
        *,
        date_from: date,
        date_to: date,
    ) -> PreparedArrestStatistics:
        cache_key = (options.faction_id, date_from, date_to)
        now = monotonic()
        async with self._cache_lock:
            report_ids = self._fresh_value(
                self._summary_cache.get(cache_key),
                now=now,
                ttl=options.cache_ttl_seconds,
            )
            cached_result = self._fresh_value(
                self._result_cache.get(cache_key),
                now=now,
                ttl=options.cache_ttl_seconds,
            )
        if report_ids is None:
            report_ids = await self._list_report_ids(
                lookup,
                date_from=date_from,
                date_to=date_to,
                max_reports=options.max_reports,
            )
            async with self._cache_lock:
                self._summary_cache[cache_key] = _TimedValue(report_ids, monotonic())
        elif len(report_ids) > options.max_reports:
            raise ArrestStatisticsTooLargeError(
                count=len(report_ids),
                limit=options.max_reports,
            )

        async with self._cache_lock:
            cached_detail_count = sum(
                self._fresh_value(
                    self._detail_cache.get((options.faction_id, report_id)),
                    now=now,
                    ttl=options.cache_ttl_seconds,
                )
                is not None
                for report_id in report_ids
            )
        return PreparedArrestStatistics(
            faction_id=options.faction_id,
            date_from=date_from,
            date_to=date_to,
            report_ids=report_ids,
            cached_detail_count=(len(report_ids) if cached_result is not None else cached_detail_count),
            estimated_requests=(0 if cached_result is not None else len(report_ids) - cached_detail_count),
            cached_result=cached_result,
        )

    async def analyze(
        self,
        lookup: ArrestReportLookup,
        options: ArrestStatisticsOptions,
        prepared: PreparedArrestStatistics,
    ) -> ArrestStatisticsResult:
        if prepared.cached_result is not None:
            return prepared.cached_result
        semaphore = asyncio.Semaphore(options.detail_concurrency)

        async def resolve(report_id: int) -> ArrestReportDetail | None:
            cache_key = (prepared.faction_id, report_id)
            now = monotonic()
            async with self._cache_lock:
                cached = self._fresh_value(
                    self._detail_cache.get(cache_key),
                    now=now,
                    ttl=options.cache_ttl_seconds,
                )
            if cached is not None:
                return cached
            async with semaphore:
                response = await lookup.get(report_id)
            if response.report is None:
                return None
            async with self._cache_lock:
                self._detail_cache[cache_key] = _TimedValue(response.report, monotonic())
            return response.report

        details = tuple(
            report
            for report in await asyncio.gather(*(resolve(report_id) for report_id in prepared.report_ids))
            if report is not None
        )
        if prepared.report_ids and not details:
            raise ArrestStatisticsUnavailableError("no arrest details were available")
        result = aggregate_arrest_statistics(
            details,
            date_from=prepared.date_from,
            date_to=prepared.date_to,
            expected_reports=len(prepared.report_ids),
        )
        if result.failed_reports == 0:
            async with self._cache_lock:
                self._result_cache[
                    (prepared.faction_id, prepared.date_from, prepared.date_to)
                ] = _TimedValue(result, monotonic())
        return result

    async def _list_report_ids(
        self,
        lookup: ArrestReportLookup,
        *,
        date_from: date,
        date_to: date,
        max_reports: int,
    ) -> tuple[int, ...]:
        page = 1
        ordered_ids: list[int] = []
        seen: set[int] = set()
        while True:
            result = await lookup.search(
                ArrestReportSearchQuery(
                    suspect_name=None,
                    officer_name=None,
                    date_from=date_from,
                    date_to=date_to,
                    page=page,
                    limit=PAGE_SIZE,
                    sort_field="created_at",
                    sort_order="desc",
                )
            )
            if result.error is not None:
                raise ArrestStatisticsUnavailableError(result.error)
            if page == 1 and result.total > max_reports:
                raise ArrestStatisticsTooLargeError(count=result.total, limit=max_reports)
            for report in result.reports:
                if report.report_id not in seen:
                    seen.add(report.report_id)
                    ordered_ids.append(report.report_id)
            if len(ordered_ids) > max_reports:
                raise ArrestStatisticsTooLargeError(count=len(ordered_ids), limit=max_reports)
            if not result.has_more:
                break
            page += 1
        return tuple(ordered_ids)

    @staticmethod
    def _fresh_value[T](
        entry: _TimedValue[T] | None,
        *,
        now: float,
        ttl: int,
    ) -> T | None:
        if entry is None or now - entry.stored_at >= ttl:
            return None
        return entry.value


def aggregate_arrest_statistics(
    reports: tuple[ArrestReportDetail, ...],
    *,
    date_from: date,
    date_to: date,
    expected_reports: int | None = None,
) -> ArrestStatisticsResult:
    """Aggregate charge appearances and exact per-report charge combinations."""

    charge_counts: Counter[tuple[str, str | None]] = Counter()
    combination_counts: Counter[tuple[tuple[str, str | None], ...]] = Counter()
    classification: Counter[str] = Counter()
    labels: dict[tuple[str, str | None], tuple[str, str | None]] = {}
    for report in reports:
        report_keys: set[tuple[str, str | None]] = set()
        for charge in report.charges:
            key = _charge_key(charge)
            labels.setdefault(key, (_clean_label(charge.name), _clean_code(charge.code)))
            charge_counts[key] += 1
            report_keys.add(key)
            classification[_classification(charge.charge_type)] += 1
        if len(report_keys) >= 2:
            combination_counts[
                tuple(sorted(report_keys, key=lambda item: (item[0], item[1] or "")))
            ] += 1

    total_charges = sum(charge_counts.values())
    ordered_charges = sorted(
        charge_counts.items(),
        key=lambda item: (-item[1], labels[item[0]][0].casefold(), labels[item[0]][1] or ""),
    )
    top_charges = tuple(
        ChargeStatistic(
            name=labels[key][0],
            code=labels[key][1],
            count=count,
            percentage=(count * 100 / total_charges if total_charges else 0.0),
        )
        for key, count in ordered_charges
    )
    top_combinations = tuple(
        ChargeCombinationStatistic(
            charges=tuple(labels[key] for key in keys),
            report_count=count,
        )
        for keys, count in sorted(
            combination_counts.items(),
            key=lambda item: (
                -item[1],
                tuple(labels[key][0].casefold() for key in item[0]),
            ),
        )
    )
    expected = len(reports) if expected_reports is None else expected_reports
    return ArrestStatisticsResult(
        date_from=date_from,
        date_to=date_to,
        total_reports=expected,
        analyzed_reports=len(reports),
        failed_reports=max(0, expected - len(reports)),
        total_charges=total_charges,
        top_charges=top_charges,
        top_combinations=top_combinations,
        minor_charges=classification["minor"],
        serious_charges=classification["serious"],
        unknown_charges=classification["unknown"],
    )


def _charge_key(charge: ArrestReportCharge) -> tuple[str, str | None]:
    return (_key(charge.name), _key(charge.code) if charge.code else None)


def _classification(value: str | None) -> str:
    normalized = _key(value or "")
    if "menor" in normalized or "no grave" in normalized:
        return "minor"
    if "grave" in normalized:
        return "serious"
    return "unknown"


def _clean_label(value: str) -> str:
    return " ".join(value.split())


def _clean_code(value: str | None) -> str | None:
    cleaned = " ".join((value or "").split()).upper()
    return cleaned or None


def _key(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return " ".join(
        "".join(char for char in decomposed if not unicodedata.combining(char)).split()
    ).casefold()
