"""Shared deterministic date-range presets for Discord forms and services."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from enum import StrEnum


class DateRangePeriod(StrEnum):
    LAST_1_DAY = "1"
    LAST_2_DAYS = "2"
    LAST_7_DAYS = "7"
    LAST_14_DAYS = "14"
    LAST_30_DAYS = "30"
    LAST_90_DAYS = "90"
    CUSTOM = "custom"


def resolve_date_range(
    period: DateRangePeriod,
    raw_date_from: str,
    raw_date_to: str,
    *,
    max_days: int,
    today: date | None = None,
) -> tuple[date, date]:
    """Resolve one preset or explicit inclusive range with common validation."""

    if max_days < 1:
        raise ValueError("max_days must be positive")
    current_date = today or datetime.now(UTC).date()
    if period is DateRangePeriod.CUSTOM:
        try:
            date_from = date.fromisoformat(raw_date_from.strip())
            date_to = date.fromisoformat(raw_date_to.strip())
        except ValueError as error:
            raise ValueError("custom dates must use ISO format") from error
    else:
        days = int(period.value)
        date_to = current_date
        date_from = current_date - timedelta(days=days - 1)
    if date_from > date_to or date_to > current_date:
        raise ValueError("invalid or future date range")
    if (date_to - date_from).days + 1 > max_days:
        raise ValueError("date range exceeds configured maximum")
    return date_from, date_to
