"""Deterministic projections for a faction-wide member analysis."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from arrest_report_agent.faction_directory import FactionActivityLevel


@dataclass(frozen=True, slots=True)
class FactionActivityCount:
    level: FactionActivityLevel
    count: int


@dataclass(frozen=True, slots=True)
class FactionDivisionCount:
    name: str
    count: int
    is_base: bool


@dataclass(frozen=True, slots=True)
class FactionMemberAnalysisSnapshot:
    faction_id: int
    generated_at: datetime
    total_members: int
    connected_members: int
    on_duty_members: int
    connected_off_duty_members: int
    activity: tuple[FactionActivityCount, ...]
    divisions: tuple[FactionDivisionCount, ...]
    unassigned_divisions: int
    invalid_divisions: int
    over_warning_days: int
    over_critical_days: int
    unknown_activity: int
    inactivity_warning_days: int
    inactivity_critical_days: int
    inactivity_warning_percentage: int
    inactivity_critical_percentage: int
    off_duty_alert_min_connected: int
    off_duty_alert_percentage: int

    def percentage(self, count: int) -> float:
        if self.total_members == 0:
            return 0.0
        return count / self.total_members * 100

    def activity_count(self, level: FactionActivityLevel) -> int:
        return next((item.count for item in self.activity if item.level is level), 0)

    @property
    def connected_off_duty_percentage(self) -> float:
        if self.connected_members == 0:
            return 0.0
        return self.connected_off_duty_members / self.connected_members * 100

    @property
    def has_off_duty_attention(self) -> bool:
        return bool(
            self.connected_members >= self.off_duty_alert_min_connected
            and self.connected_off_duty_percentage >= self.off_duty_alert_percentage
        )

    @property
    def base_division_count(self) -> int:
        return sum(item.count for item in self.divisions if item.is_base)

    @property
    def specialty_count(self) -> int:
        return sum(item.count for item in self.divisions if not item.is_base)

    @property
    def has_attention_items(self) -> bool:
        return any(
            (
                self.over_warning_days,
                self.over_critical_days,
                self.unknown_activity,
                self.unassigned_divisions,
                self.invalid_divisions,
                self.has_off_duty_attention,
            )
        )

    @property
    def is_critical(self) -> bool:
        return bool(
            self.over_critical_days
            or self.percentage(self.over_warning_days) >= self.inactivity_critical_percentage
            or self.invalid_divisions
        )
