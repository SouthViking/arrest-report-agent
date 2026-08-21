"""Application models for SARP vehicle-theft reports."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from arrest_report_agent.infrastructure.sarp import SarpStolenVehicle


class StolenVehicleStatusFilter(StrEnum):
    ACTIVE = "active"
    RESOLVED = "resolved"
    ALL = "all"

    @property
    def api_state(self) -> bool | None:
        if self is StolenVehicleStatusFilter.ACTIVE:
            return True
        if self is StolenVehicleStatusFilter.RESOLVED:
            return False
        return None

    @property
    def label(self) -> str:
        return {
            StolenVehicleStatusFilter.ACTIVE: "Activos",
            StolenVehicleStatusFilter.RESOLVED: "Resueltos",
            StolenVehicleStatusFilter.ALL: "Todos",
        }[self]


@dataclass(frozen=True, slots=True)
class StolenVehicleQueryResult:
    items: tuple[SarpStolenVehicle, ...]
    requested_count: int
    status: StolenVehicleStatusFilter
    available_count: int
