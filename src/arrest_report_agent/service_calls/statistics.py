"""Deterministic date ranges and counters for calls retained in Discord."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from arrest_report_agent.application.timezones import parse_local_datetime


class CallCategory(StrEnum):
    POLICE = "police"
    FIRE_MEDICAL = "fire_medical"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class ServiceCallStatisticsQuery:
    starts_at: datetime
    ends_at: datetime
    timezone_key: str

    @classmethod
    def parse(
        cls,
        raw_from: str,
        raw_to: str,
        timezone_key: str,
        *,
        max_days: int,
        now: datetime | None = None,
    ) -> ServiceCallStatisticsQuery:
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("now must include timezone information")
        current = current.astimezone(UTC)
        start = parse_local_datetime(raw_from, timezone_key)
        end = parse_local_datetime(raw_to, timezone_key).replace(second=59, microsecond=999999)
        if end.replace(second=0, microsecond=0) == current.replace(second=0, microsecond=0):
            end = current
        if start > end:
            raise ValueError("El inicio no puede ser posterior al término.")
        if end > current:
            raise ValueError("No puedes consultar fechas u horas futuras.")
        if end.replace(second=0, microsecond=0) - start > timedelta(days=max_days):
            raise ValueError(f"El rango máximo es de {max_days} días.")
        return cls(start, end, timezone_key)

    def includes(self, timestamp: datetime) -> bool:
        return self.starts_at <= timestamp <= self.ends_at


@dataclass(frozen=True, slots=True)
class ServiceCallStatisticsResult:
    query: ServiceCallStatisticsQuery
    police: int
    fire_medical: int
    other: int
    channel_ids: tuple[int, ...]
    messages_scanned: int
    late_delivery_hours: int
    truncated: bool = False
    unavailable: bool = False
    invalid_timestamps: int = 0

    @property
    def total(self) -> int:
        return self.police + self.fire_medical + self.other

    @property
    def incomplete(self) -> bool:
        return self.truncated or self.unavailable or self.invalid_timestamps > 0

    def percentage(self, count: int) -> str:
        percentage = 100 * count / self.total if self.total else 0
        return f"{percentage:.1f}".removesuffix(".0").replace(".", ",")
