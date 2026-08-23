"""Shared bounded retrieval and deterministic normalization of SARP radio logs."""

from __future__ import annotations

import re
import unicodedata
from datetime import date
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
        date_from: date,
        date_to: date,
        slot: int | None,
        member_query: str | None,
        max_raw_records: int,
        newest_first: bool,
    ) -> RadioLogQueryResult:
        if max_raw_records < 1:
            raise ValueError("max_raw_records must be positive")
        if slot is not None and (isinstance(slot, bool) or slot <= 0):
            raise ValueError("slot must be a positive integer")
        normalized_member = _normalize_optional_member(member_query)
        first = await self.client.list_faction_radio_logs(
            faction_id,
            date_from=date_from,
            date_to=date_to,
            page=1,
            limit=min(TRANSPORT_PAGE_SIZE, max_raw_records),
            search=normalized_member,
            sort_order="desc",
        )
        if first.total > max_raw_records:
            raise RadioLogScanTooLargeError(total=first.total, limit=max_raw_records)

        raw_rows = list(first.data)
        page_size = min(TRANSPORT_PAGE_SIZE, max_raw_records)
        for page in range(2, ceil(first.total / page_size) + 1):
            result = await self.client.list_faction_radio_logs(
                faction_id,
                date_from=date_from,
                date_to=date_to,
                page=page,
                limit=page_size,
                search=normalized_member,
                sort_order="desc",
            )
            raw_rows.extend(result.data)

        deduplicated = {row.id: row for row in raw_rows}
        parsed = tuple(_parse_radio_log(row) for row in deduplicated.values())
        filtered = tuple(
            row for row in parsed if _matches_slot(row, slot) and _matches_member(row, normalized_member)
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
            raw_total=first.total,
            scanned_count=len(deduplicated),
            malformed_count=sum(row.malformed for row in parsed),
            items=ordered,
        )


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
