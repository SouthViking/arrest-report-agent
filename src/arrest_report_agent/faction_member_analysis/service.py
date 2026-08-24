"""Build a cached, deterministic faction-member analysis."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from arrest_report_agent.config import FactionMemberAnalysisOptions
from arrest_report_agent.faction_directory import (
    FactionActivityLevel,
    FactionDirectoryService,
    activity_level_for,
)

from .models import (
    FactionActivityCount,
    FactionDivisionCount,
    FactionMemberAnalysisSnapshot,
)


class FactionMemberAnalysisService:
    """Aggregate one roster and its ordinal division catalog without per-member calls."""

    def __init__(self, directory: FactionDirectoryService) -> None:
        self.directory = directory

    async def analyze(
        self,
        options: FactionMemberAnalysisOptions,
        *,
        now: datetime | None = None,
    ) -> FactionMemberAnalysisSnapshot:
        current = now or datetime.now(UTC)
        current = current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)
        roster, catalog = await asyncio.gather(
            self.directory.roster(options.faction_id),
            self.directory.divisions(options.faction_id),
        )
        activity_counts = {level: 0 for level in FactionActivityLevel}
        division_counts = [0 for _division in catalog]
        base_names = {name.strip().casefold() for name in options.base_divisions}
        unassigned = 0
        invalid_divisions = 0
        over_warning_days = 0
        over_critical_days = 0

        for member in roster:
            level = activity_level_for(member.last_connected_at, now=current)
            activity_counts[level] += 1
            if member.last_connected_at is not None:
                age = _age(member.last_connected_at, current)
                if age > timedelta(days=options.inactivity_warning_days):
                    over_warning_days += 1
                if age > timedelta(days=options.inactivity_critical_days):
                    over_critical_days += 1
            if member.division == 0:
                unassigned += 1
            elif member.division > len(catalog):
                invalid_divisions += 1
            else:
                division_counts[member.division - 1] += 1
        divisions = tuple(
            FactionDivisionCount(
                name=division.name,
                count=division_counts[index],
                is_base=division.name.strip().casefold() in base_names,
            )
            for index, division in enumerate(catalog)
        )
        return FactionMemberAnalysisSnapshot(
            faction_id=options.faction_id,
            generated_at=current,
            total_members=len(roster),
            connected_members=sum(member.online for member in roster),
            on_duty_members=sum(member.factionduty for member in roster),
            connected_off_duty_members=sum(
                member.online and not member.factionduty for member in roster
            ),
            activity=tuple(
                FactionActivityCount(level=level, count=activity_counts[level])
                for level in FactionActivityLevel
            ),
            divisions=divisions,
            unassigned_divisions=unassigned,
            invalid_divisions=invalid_divisions,
            over_warning_days=over_warning_days,
            over_critical_days=over_critical_days,
            unknown_activity=activity_counts[FactionActivityLevel.UNKNOWN],
            inactivity_warning_days=options.inactivity_warning_days,
            inactivity_critical_days=options.inactivity_critical_days,
            inactivity_warning_percentage=options.inactivity_warning_percentage,
            inactivity_critical_percentage=options.inactivity_critical_percentage,
            off_duty_alert_min_connected=options.off_duty_alert_min_connected,
            off_duty_alert_percentage=options.off_duty_alert_percentage,
        )


def _age(last_connected_at: datetime, now: datetime) -> timedelta:
    connected = (
        last_connected_at.replace(tzinfo=UTC)
        if last_connected_at.tzinfo is None
        else last_connected_at.astimezone(UTC)
    )
    return max(timedelta(), now - connected)
