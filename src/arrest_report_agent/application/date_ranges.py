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


class DateTimeRangePeriod(StrEnum):
    """Rolling UTC intervals shared by commands that need hour-level precision."""

    LAST_1_HOUR = "1h"
    LAST_2_HOURS = "2h"
    LAST_4_HOURS = "4h"
    LAST_8_HOURS = "8h"
    LAST_1_DAY = "1d"
    LAST_2_DAYS = "2d"
    LAST_7_DAYS = "7d"
    LAST_14_DAYS = "14d"
    LAST_30_DAYS = "30d"
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


def resolve_compact_date_range(
    period: DateRangePeriod,
    raw_range: str,
    *,
    max_days: int,
    today: date | None = None,
) -> tuple[date, date]:
    """Resolve presets or one compact ``AAAA-MM-DD / AAAA-MM-DD`` form value."""

    if period is not DateRangePeriod.CUSTOM:
        return resolve_date_range(period, "", "", max_days=max_days, today=today)
    parts = [part.strip() for part in raw_range.split("/")]
    if len(parts) != 2 or not all(parts):
        raise ValueError("custom range must contain two ISO dates separated by slash")
    return resolve_date_range(
        period,
        parts[0],
        parts[1],
        max_days=max_days,
        today=today,
    )


def resolve_compact_datetime_range(
    period: DateTimeRangePeriod,
    raw_range: str,
    *,
    max_days: int,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Resolve a rolling preset or ``AAAA-MM-DD HH:MM / ...`` UTC interval."""

    if max_days < 1:
        raise ValueError("max_days must be positive")
    current_time = _as_utc(now or datetime.now(UTC)).replace(microsecond=0)
    if period is DateTimeRangePeriod.CUSTOM:
        parts = [part.strip() for part in raw_range.split("/")]
        if len(parts) != 2 or not all(parts):
            raise ValueError("custom range must contain two UTC datetimes separated by slash")
        try:
            date_from = datetime.strptime(parts[0], "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
            date_to = datetime.strptime(parts[1], "%Y-%m-%d %H:%M").replace(
                tzinfo=UTC,
                second=59,
                microsecond=999_999,
            )
        except ValueError as error:
            raise ValueError("custom datetimes must use ISO minute format") from error
        if date_to.replace(second=0, microsecond=0) == current_time.replace(
            second=0,
            microsecond=0,
        ):
            date_to = current_time
    else:
        amount = int(period.value[:-1])
        unit = period.value[-1]
        duration = timedelta(hours=amount) if unit == "h" else timedelta(days=amount)
        date_from = current_time - duration
        date_to = current_time
    if date_from > date_to or date_to > current_time:
        raise ValueError("invalid or future datetime range")
    validation_end = date_to.replace(second=0, microsecond=0)
    if validation_end - date_from > timedelta(days=max_days):
        raise ValueError("datetime range exceeds configured maximum")
    return date_from, date_to


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("now must include timezone information")
    return value.astimezone(UTC)
