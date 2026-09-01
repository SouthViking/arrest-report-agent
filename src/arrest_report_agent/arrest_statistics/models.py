"""Deterministic arrest-statistics domain models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class ChargeStatistic:
    name: str
    code: str | None
    count: int
    percentage: float


@dataclass(frozen=True, slots=True)
class ChargeCombinationStatistic:
    charges: tuple[tuple[str, str | None], ...]
    report_count: int


@dataclass(frozen=True, slots=True)
class ArrestStatisticsResult:
    date_from: date
    date_to: date
    total_reports: int
    analyzed_reports: int
    failed_reports: int
    total_charges: int
    top_charges: tuple[ChargeStatistic, ...]
    top_combinations: tuple[ChargeCombinationStatistic, ...]
    minor_charges: int
    serious_charges: int
    unknown_charges: int


@dataclass(frozen=True, slots=True)
class PreparedArrestStatistics:
    faction_id: int
    date_from: date
    date_to: date
    report_ids: tuple[int, ...]
    cached_detail_count: int
    estimated_requests: int
    cached_result: ArrestStatisticsResult | None = None

    @property
    def report_count(self) -> int:
        return len(self.report_ids)


class ArrestStatisticsTooLargeError(RuntimeError):
    def __init__(self, *, count: int, limit: int) -> None:
        super().__init__(f"arrest analysis contains {count} reports; limit is {limit}")
        self.count = count
        self.limit = limit


class ArrestStatisticsUnavailableError(RuntimeError):
    """Raised when no report detail could be analyzed."""

