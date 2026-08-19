"""Deterministic projections shared by faction-facing features."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum


class FactionActivityLevel(StrEnum):
    VERY_ACTIVE = "Muy activo"
    ACTIVE = "Activo"
    REGULAR = "Regular"
    AT_RISK = "En riesgo"
    INACTIVE = "Inactivo"
    UNKNOWN = "Sin datos"

    @property
    def counts_as_active(self) -> bool:
        return self in {
            FactionActivityLevel.VERY_ACTIVE,
            FactionActivityLevel.ACTIVE,
            FactionActivityLevel.REGULAR,
        }


@dataclass(frozen=True, slots=True)
class FactionDirectoryMember:
    name: str
    online: bool
    on_duty: bool
    rank_name: str
    rank_order: int
    salary: int | float | None
    last_connected_at: datetime | None
    faction_time_minutes: int

    def activity_level(self, *, now: datetime | None = None) -> FactionActivityLevel:
        return activity_level_for(self.last_connected_at, now=now)


@dataclass(frozen=True, slots=True)
class FactionDirectorySnapshot:
    faction_id: int
    members: tuple[FactionDirectoryMember, ...]


def activity_level_for(
    last_connected_at: datetime | None,
    *,
    now: datetime | None = None,
) -> FactionActivityLevel:
    """Reproduce the PCU activity bands from the last connection timestamp."""

    if last_connected_at is None:
        return FactionActivityLevel.UNKNOWN
    current = now or datetime.now(UTC)
    current = current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)
    connected_at = (
        last_connected_at.replace(tzinfo=UTC)
        if last_connected_at.tzinfo is None
        else last_connected_at.astimezone(UTC)
    )
    age = max(timedelta(), current - connected_at)
    if age <= timedelta(days=3):
        return FactionActivityLevel.VERY_ACTIVE
    if age <= timedelta(days=7):
        return FactionActivityLevel.ACTIVE
    if age <= timedelta(days=14):
        return FactionActivityLevel.REGULAR
    if age <= timedelta(days=21):
        return FactionActivityLevel.AT_RISK
    return FactionActivityLevel.INACTIVE
