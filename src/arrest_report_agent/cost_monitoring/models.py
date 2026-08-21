"""Provider-neutral snapshots for the internal cost monitor."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True, slots=True)
class OpenAICostSnapshot:
    """Current OpenAI project or organization spend."""

    current_spend: Decimal
    hard_limit: Decimal | None
    remaining: Decimal | None
    limit_scope: Literal["project", "organization"]


@dataclass(frozen=True, slots=True)
class RailwayCostSnapshot:
    """Current Railway project/workspace usage and workspace-enforced limit."""

    current_usage: Decimal | None
    estimated_bill: Decimal | None
    hard_limit: Decimal | None
    remaining: Decimal | None
    usage_scope: Literal["project", "workspace"] = "workspace"


@dataclass(frozen=True, slots=True)
class CostMonitoringSnapshot:
    """A point-in-time view assembled from the available providers."""

    fetched_at: datetime
    openai: OpenAICostSnapshot | None = None
    railway: RailwayCostSnapshot | None = None

    @property
    def has_data(self) -> bool:
        return self.openai is not None or self.railway is not None
