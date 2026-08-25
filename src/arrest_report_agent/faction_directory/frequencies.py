"""Failure-safe, faction-bound frequency lookup for agent tools."""

from __future__ import annotations

import logging

from arrest_report_agent.application.ports import (
    FactionFrequencyLookupResult,
    FactionFrequencyProfile,
)
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.sarp import (
    SarpError,
    SarpNotFoundError,
    SarpRateLimitError,
)

from .service import FactionDirectoryService, display_name

LOGGER = logging.getLogger(__name__)


class SarpFactionFrequencyLookup:
    """Bind the shared directory cache to one configured faction."""

    def __init__(self, directory: FactionDirectoryService, *, faction_id: int) -> None:
        if faction_id <= 0:
            raise ValueError("faction_id must be positive")
        self.directory = directory
        self.faction_id = faction_id

    async def list(self) -> FactionFrequencyLookupResult:
        try:
            frequencies = await self.directory.frequencies(self.faction_id)
        except Exception as error:
            code, retry_after = _safe_failure(error)
            log_event(
                LOGGER,
                "warning",
                "agent_faction_frequency_lookup_failed",
                faction_id=self.faction_id,
                failure=code,
                exc_info=error,
                **exception_fields(error),
            )
            return FactionFrequencyLookupResult(
                frequencies=(),
                division_catalog_complete=False,
                error=code,
                retry_after_seconds=retry_after,
            )

        division_catalog_complete = True
        try:
            divisions = await self.directory.divisions(self.faction_id)
        except Exception as error:
            divisions = ()
            division_catalog_complete = False
            log_event(
                LOGGER,
                "warning",
                "agent_faction_frequency_divisions_unavailable",
                faction_id=self.faction_id,
                frequency_count=len(frequencies),
                exc_info=error,
                **exception_fields(error),
            )
        division_names = {division.id: display_name(division.name) for division in divisions}
        if any(
            frequency.division > 0 and frequency.division not in division_names
            for frequency in frequencies
        ):
            division_catalog_complete = False
        profiles = tuple(
            sorted(
                (
                    FactionFrequencyProfile(
                        frequency=frequency.frequency,
                        name=display_name(frequency.name),
                        division_name=division_names.get(frequency.division),
                    )
                    for frequency in frequencies
                ),
                key=lambda item: (item.frequency, item.name.casefold()),
            )
        )
        log_event(
            LOGGER,
            "info",
            "agent_faction_frequency_lookup_succeeded",
            faction_id=self.faction_id,
            frequency_count=len(profiles),
            division_catalog_complete=division_catalog_complete,
        )
        return FactionFrequencyLookupResult(
            frequencies=profiles,
            division_catalog_complete=division_catalog_complete,
        )


def _safe_failure(error: Exception) -> tuple[str, float | None]:
    if isinstance(error, SarpNotFoundError):
        return "not_found", None
    if isinstance(error, SarpRateLimitError):
        return "rate_limited", error.rate_limit.retry_after_seconds
    if isinstance(error, SarpError):
        return "temporarily_unavailable", None
    return "temporarily_unavailable", None
