"""Deterministic arrest charge analytics."""

from .discord import ArrestStatisticsCog
from .models import (
    ArrestStatisticsResult,
    ArrestStatisticsTooLargeError,
    ArrestStatisticsUnavailableError,
    ChargeCombinationStatistic,
    ChargeStatistic,
    PreparedArrestStatistics,
)
from .service import ArrestStatisticsService, aggregate_arrest_statistics

__all__ = [
    "ArrestStatisticsCog",
    "ArrestStatisticsResult",
    "ArrestStatisticsService",
    "ArrestStatisticsTooLargeError",
    "ArrestStatisticsUnavailableError",
    "ChargeCombinationStatistic",
    "ChargeStatistic",
    "PreparedArrestStatistics",
    "aggregate_arrest_statistics",
]
