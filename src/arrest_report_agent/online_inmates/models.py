"""Deterministic projections for connected characters in prison."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OnlineInmateSnapshot:
    """Current server population and the connected characters in prison."""

    server_connected_count: int
    inmate_names: tuple[str, ...]

    @property
    def inmate_count(self) -> int:
        return len(self.inmate_names)
