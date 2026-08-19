"""Assemble privacy-minimized faction reports from shared cached SARP data."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime
from time import monotonic
from typing import Protocol

from arrest_report_agent.faction_directory import FactionDirectoryService
from arrest_report_agent.infrastructure.sarp import SarpFactionBankEntry, SarpFactionBankPage

from .models import FactionReport, FactionReportMember, FactionReportTooLargeError

PAGE_SIZE = 500
DEFAULT_BANK_CACHE_SECONDS = 60.0
DEFAULT_MAX_BANK_RECORDS = 10_000


class FactionBankApiClient(Protocol):
    async def list_faction_bank_logs(
        self,
        faction_id: int,
        *,
        date_from: date,
        date_to: date,
        page: int = 1,
        limit: int = PAGE_SIZE,
    ) -> SarpFactionBankPage: ...


@dataclass(frozen=True, slots=True)
class _BankSnapshot:
    total: int
    entries: tuple[SarpFactionBankEntry, ...]


class FactionReportService:
    """Build reports while coalescing identical bank reads and sharing the roster cache."""

    def __init__(
        self,
        client: FactionBankApiClient,
        directory: FactionDirectoryService,
        *,
        bank_cache_seconds: float = DEFAULT_BANK_CACHE_SECONDS,
    ) -> None:
        if bank_cache_seconds < 0:
            raise ValueError("Faction bank cache duration cannot be negative")
        self.client = client
        self.directory = directory
        self.bank_cache_seconds = bank_cache_seconds
        self._bank_cache: dict[tuple[int, date, date], tuple[float, _BankSnapshot]] = {}
        self._bank_inflight: dict[tuple[int, date, date], asyncio.Task[_BankSnapshot]] = {}
        self._lock = asyncio.Lock()

    async def generate(
        self,
        *,
        faction_id: int,
        date_from: date,
        date_to: date,
        max_bank_records: int = DEFAULT_MAX_BANK_RECORDS,
        now: datetime | None = None,
    ) -> FactionReport:
        if faction_id <= 0:
            raise ValueError("faction_id must be positive")
        if date_from > date_to:
            raise ValueError("date_from cannot be after date_to")
        if max_bank_records < 1:
            raise ValueError("max_bank_records must be positive")
        current = now or datetime.now(UTC)
        current = current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)
        directory, bank = await asyncio.gather(
            self.directory.snapshot(faction_id),
            self._bank_snapshot(faction_id, date_from, date_to, max_bank_records),
        )
        members = tuple(
            FactionReportMember(
                name=member.name,
                rank_name=member.rank_name,
                rank_order=member.rank_order,
                salary=member.salary,
                activity=activity,
                faction_time_minutes=member.faction_time_minutes,
            )
            for member in directory.members
            if (activity := member.activity_level(now=current)).counts_as_active
        )
        return FactionReport(
            faction_id=faction_id,
            date_from=date_from,
            date_to=date_to,
            generated_at=current,
            bank_entries=bank.entries,
            active_members=members,
            total_members=len(directory.members),
        )

    async def _bank_snapshot(
        self,
        faction_id: int,
        date_from: date,
        date_to: date,
        max_records: int,
    ) -> _BankSnapshot:
        key = (faction_id, date_from, date_to)
        now = monotonic()
        async with self._lock:
            cached = self._bank_cache.get(key)
            if cached is not None and cached[0] > now:
                if cached[1].total > max_records:
                    raise FactionReportTooLargeError(cached[1].total, max_records)
                return cached[1]
            task = self._bank_inflight.get(key)
            if task is None:
                task = asyncio.create_task(
                    self._fetch_bank(faction_id, date_from, date_to, max_records),
                    name=f"sarp-faction-bank-{faction_id}-{date_from}-{date_to}",
                )
                self._bank_inflight[key] = task
        try:
            snapshot = await asyncio.shield(task)
        finally:
            if task.done():
                async with self._lock:
                    if self._bank_inflight.get(key) is task:
                        self._bank_inflight.pop(key, None)
        if self.bank_cache_seconds > 0:
            async with self._lock:
                self._bank_cache[key] = (
                    monotonic() + self.bank_cache_seconds,
                    snapshot,
                )
        if snapshot.total > max_records:
            raise FactionReportTooLargeError(snapshot.total, max_records)
        return snapshot

    async def _fetch_bank(
        self,
        faction_id: int,
        date_from: date,
        date_to: date,
        max_records: int,
    ) -> _BankSnapshot:
        entries: list[SarpFactionBankEntry] = []
        page = 1
        total = 0
        while True:
            payload = await self.client.list_faction_bank_logs(
                faction_id,
                date_from=date_from,
                date_to=date_to,
                page=page,
                limit=PAGE_SIZE,
            )
            total = payload.total
            if total > max_records:
                raise FactionReportTooLargeError(total, max_records)
            entries.extend(payload.data)
            if len(entries) >= total or not payload.data:
                break
            page += 1
        return _BankSnapshot(
            total=total,
            entries=tuple(sorted(entries, key=lambda item: (item.created_at, item.id), reverse=True)),
        )
