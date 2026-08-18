"""Deterministic projections for live faction presence."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FactionPresenceMember:
    name: str
    on_duty: bool
    rank_name: str = "Rango desconocido"
    rank_order: int = -1


@dataclass(frozen=True, slots=True)
class FactionPresenceSnapshot:
    faction_id: int
    total_members: int
    connected_members: tuple[FactionPresenceMember, ...]

    @property
    def connected_count(self) -> int:
        return len(self.connected_members)

    @property
    def on_duty_count(self) -> int:
        return sum(member.on_duty for member in self.connected_members)

    @property
    def duty_percentage(self) -> int:
        if not self.connected_members:
            return 0
        return round(self.on_duty_count / self.connected_count * 100)
