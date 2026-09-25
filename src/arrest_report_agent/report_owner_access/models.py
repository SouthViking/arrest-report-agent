"""Stable result types for report-owner agent quotas."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class OwnerRunStatus(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"


class OwnerRunReservationResult(StrEnum):
    RESERVED = "reserved"
    EXHAUSTED = "exhausted"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class OwnerRunReservation:
    result: OwnerRunReservationResult
    used_rounds: int
    max_rounds: int
    run_id: UUID | None = None

    @property
    def round_number(self) -> int | None:
        return self.used_rounds if self.result is OwnerRunReservationResult.RESERVED else None


@dataclass(frozen=True, slots=True)
class OwnerRoundGrant:
    """Updated quota after one idempotent, audited administrator grant."""

    added_rounds: int
    used_rounds: int
    max_rounds: int
    duplicate: bool = False

    @property
    def remaining_rounds(self) -> int:
        return max(0, self.max_rounds - self.used_rounds)
