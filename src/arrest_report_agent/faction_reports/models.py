"""Deterministic report projections independent from Discord and XLSX."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from arrest_report_agent.faction_directory import FactionActivityLevel
from arrest_report_agent.infrastructure.sarp import SarpFactionBankEntry


@dataclass(frozen=True, slots=True)
class FactionReportMember:
    name: str
    rank_name: str
    rank_order: int
    salary: int | float | None
    activity: FactionActivityLevel
    faction_time_minutes: int


@dataclass(frozen=True, slots=True)
class FactionReport:
    faction_id: int
    date_from: date
    date_to: date
    generated_at: datetime
    bank_entries: tuple[SarpFactionBankEntry, ...]
    active_members: tuple[FactionReportMember, ...]
    total_members: int


class FactionReportTooLargeError(RuntimeError):
    def __init__(self, total: int, limit: int) -> None:
        self.total = total
        self.limit = limit
        super().__init__(f"Faction report contains {total} bank entries; limit is {limit}")
