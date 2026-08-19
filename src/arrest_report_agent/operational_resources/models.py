"""Deterministic projections for the operational resources panel."""

from __future__ import annotations

from dataclasses import dataclass

from arrest_report_agent.faction_status import FactionPresenceSnapshot
from arrest_report_agent.infrastructure.sarp import SarpServerCacheInfo


@dataclass(frozen=True, slots=True)
class OperationalResourcesSnapshot:
    faction_status: FactionPresenceSnapshot
    server_cache: SarpServerCacheInfo | None
