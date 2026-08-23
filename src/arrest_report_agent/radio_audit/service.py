"""Shared bounded retrieval and deterministic normalization of SARP radio logs."""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
from math import ceil

from arrest_report_agent.infrastructure.sarp import SarpClient, SarpFactionRadioLog

from .models import ParsedRadioLog, RadioLogQueryResult, RadioLogScanTooLargeError

RADIO_PREFIX_RE = re.compile(
    r"^\s*\*{0,2}\[\s*CH\s*:\s*(?P<channel>[^,\]]+)\s*,\s*"
    r"S\s*:\s*(?P<slot>\d+)\s*\]\*{0,2}\s*"
    r"(?P<speaker>[^:]+?)\s*:\s*(?P<body>.+?)\s*$",
    flags=re.IGNORECASE | re.DOTALL,
)
TRANSPORT_PAGE_SIZE = 500


class RadioLogService:
    """Fetch a bounded SARP sample once and reuse it across all consumers."""

    def __init__(self, client: SarpClient) -> None:
        self.client = client

    async def query(
        self,
        *,
        faction_id: int,
        date_from: datetime,
        date_to: datetime,
        slot: int | None,
        member_query: str | None,
        max_raw_records: int,
        newest_first: bool,
    ) -> RadioLogQueryResult:
        if max_raw_records < 1:
            raise ValueError("max_raw_records must be positive")
        date_from = _as_utc(date_from)
        date_to = _as_utc(date_to)
        if date_from > date_to:
            raise ValueError("date_from cannot be after date_to")
        if slot is not None and (isinstance(slot, bool) or slot <= 0):
            raise ValueError("slot must be a positive integer")
        normalized_member = _normalize_optional_member(member_query)
        page_size = _bounded_page_size(max_raw_records)
        raw_rows: list[SarpFactionRadioLog] = []
        first_total = 0
        page = 1
        while True:
            result = await self.client.list_faction_radio_logs(
                faction_id,
                date_from=date_from.date(),
                date_to=date_to.date(),
                page=page,
                limit=page_size,
                search=normalized_member,
                sort_order="desc",
            )
            if page == 1:
                first_total = result.total
            raw_rows.extend(result.data)
            total_pages = ceil(result.total / page_size)
            if not result.data or _page_reaches_interval_start(result.data, date_from) or page >= total_pages:
                break
            next_page_rows = min(page_size, max(0, result.total - page * page_size))
            if len(raw_rows) + next_page_rows > max_raw_records:
                raise RadioLogScanTooLargeError(total=first_total, limit=max_raw_records)
            page += 1

        deduplicated = {row.id: row for row in raw_rows}
        parsed = tuple(_parse_radio_log(row) for row in deduplicated.values())
        interval_rows = tuple(
            row
            for row in parsed
            if _within_interval(row, date_from, date_to)
            and _matches_member(row, normalized_member)
        )
        filtered = tuple(
            row for row in interval_rows if _matches_slot(row, slot)
        )
        ordered = tuple(
            sorted(
                filtered,
                key=lambda row: (row.created_at, row.id),
                reverse=newest_first,
            )
        )
        return RadioLogQueryResult(
            faction_id=faction_id,
            date_from=date_from.isoformat(),
            date_to=date_to.isoformat(),
            slot=slot,
            member_query=normalized_member,
            raw_total=first_total,
            scanned_count=len(deduplicated),
            malformed_count=sum(row.malformed for row in interval_rows),
            items=ordered,
        )


def _bounded_page_size(max_raw_records: int) -> int:
    return min(TRANSPORT_PAGE_SIZE, max_raw_records)


def _page_reaches_interval_start(
    rows: list[SarpFactionRadioLog],
    date_from: datetime,
) -> bool:
    return min(_as_utc(row.created_at) for row in rows) < date_from


def _within_interval(
    row: ParsedRadioLog,
    date_from: datetime,
    date_to: datetime,
) -> bool:
    created_at = _as_utc(row.created_at)
    return date_from <= created_at <= date_to


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_radio_log(row: SarpFactionRadioLog) -> ParsedRadioLog:
    match = RADIO_PREFIX_RE.match(row.message)
    if match is None:
        return ParsedRadioLog(
            id=row.id,
            faction_id=row.faction,
            character_id=row.character_id,
            character_name=_display_name(row.character_name),
            created_at=row.created_at,
            raw_message=row.message,
            body=row.message.strip(),
            malformed=True,
        )
    return ParsedRadioLog(
        id=row.id,
        faction_id=row.faction,
        character_id=row.character_id,
        character_name=_display_name(row.character_name),
        created_at=row.created_at,
        raw_message=row.message,
        channel=" ".join(match.group("channel").split()),
        slot=int(match.group("slot")),
        speaker=" ".join(match.group("speaker").replace("_", " ").split()),
        body=match.group("body").strip(),
        malformed=False,
    )


def _matches_slot(row: ParsedRadioLog, slot: int | None) -> bool:
    if slot is None:
        return True
    return not row.malformed and row.slot == slot


def _matches_member(row: ParsedRadioLog, query: str | None) -> bool:
    if query is None:
        return True
    needle = _normalize(query)
    return needle in _normalize(row.character_name) or (
        row.speaker is not None and needle in _normalize(row.speaker)
    )


def _normalize_optional_member(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.replace("_", " ").split())
    if not normalized:
        return None
    if len(normalized) > 100:
        raise ValueError("member_query must contain at most 100 characters")
    return normalized


def _display_name(value: str) -> str:
    return " ".join(value.replace("_", " ").split())


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    unaccented = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(unaccented.casefold().replace("_", " ").split())
