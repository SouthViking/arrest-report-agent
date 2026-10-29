"""Failure-safe batch lookup and optional internal follow-up for weapon thefts."""

import logging
from contextlib import suppress
from typing import Protocol

from arrest_report_agent.application.ports import WeaponTheftSerialResult
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.domain import ReviewCase
from arrest_report_agent.domain.weapon_serials import normalize_serial

from .repository import StolenWeaponRepository

LOGGER = logging.getLogger(__name__)


class TheftNotifier(Protocol):
    async def notify(self, case: ReviewCase, serials_by_bag: dict[int, tuple[str, ...]]) -> None: ...


class WeaponTheftLookupService:
    def __init__(self, repository: StolenWeaponRepository) -> None:
        self.repository = repository
        self.notifier: TheftNotifier | None = None

    async def lookup(self, guild_id: int, serials: tuple[str, ...]) -> tuple[WeaponTheftSerialResult, ...]:
        valid: dict[str, str] = {}
        for serial in serials:
            with suppress(ValueError):
                valid[serial] = normalize_serial(serial)
        try:
            reports = await self.repository.find_by_serials(guild_id, tuple(dict.fromkeys(valid.values())))
        except Exception as error:
            log_event(
                LOGGER,
                "warning",
                "weapon_theft_lookup_unavailable",
                guild_id=guild_id,
                **exception_fields(error),
            )
            return tuple(
                WeaponTheftSerialResult(serial, "unavailable" if serial in valid else "invalid_serial")
                for serial in serials
            )
        results: list[WeaponTheftSerialResult] = []
        for serial in serials:
            if serial not in valid:
                results.append(WeaponTheftSerialResult(serial, "invalid_serial"))
                continue
            matches = tuple(report for report in reports if report.details.normalized_serial == valid[serial])
            status = (
                "reported_stolen"
                if any(report.status == "open" for report in matches)
                else ("closed" if matches else "no_records")
            )
            results.append(
                WeaponTheftSerialResult(
                    serial,
                    status,  # type: ignore[arg-type]
                    tuple(
                        {
                            "reference": report.reference,
                            "status": report.status,
                            "stolen_on": report.details.stolen_on.isoformat(),
                            "victim_name": report.details.victim_name,
                            "reporting_officer_name": report.details.reporting_officer_name,
                            "description": report.details.description,
                            "registered_by_user_id": str(report.registered_by),
                            "registered_at": report.created_at.isoformat(),
                            "evidence_count": len(report.assets),
                            "closed_at": report.closed_at.isoformat() if report.closed_at else None,
                            "closure_reason": report.closure_reason,
                        }
                        for report in matches
                    ),
                )
            )
        return tuple(results)

    async def notify(self, case: ReviewCase, serials_by_bag: dict[int, tuple[str, ...]]) -> None:
        if self.notifier is not None:
            await self.notifier.notify(case, serials_by_bag)
