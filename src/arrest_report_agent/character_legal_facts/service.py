"""Application facade for bounded, selective character legal-fact checks."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Protocol

from arrest_report_agent.application.ports import (
    CharacterLegalFactResult,
    CharacterLegalFactsQuery,
    CharacterLegalFactsResult,
    CharacterLegalFactStatus,
    OverdueUnpaidFineMatch,
    PriorSeriousConvictionMatch,
)
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.sarp import (
    SarpArrestPage,
    SarpCharacterSearchResult,
    SarpFinePage,
)
from arrest_report_agent.infrastructure.sarp.character_resolution import (
    CharacterAmbiguousError,
    CharacterNotFoundError,
    normalize_character_name,
    resolve_exact_character,
)
from arrest_report_agent.infrastructure.sarp.failures import classify_sarp_failure

LOGGER = logging.getLogger(__name__)
PAGE_LIMIT = 100
MAX_PAGES_PER_CHECK = 10
OVERDUE_FINE_DAYS = 3


class CharacterLegalFactsClient(Protocol):
    async def search_characters(self, name: str) -> list[SarpCharacterSearchResult]: ...

    async def list_arrests(
        self,
        character_id: int,
        *,
        page: int = 1,
        limit: int = 3,
    ) -> SarpArrestPage: ...

    async def list_unpaid_fines(
        self,
        character_id: int,
        *,
        page: int = 1,
        limit: int = 100,
    ) -> SarpFinePage: ...


class SarpCharacterLegalFactsLookup:
    """Resolve a character once and compute only explicitly requested facts."""

    def __init__(self, client: CharacterLegalFactsClient) -> None:
        self.client = client

    async def check(self, query: CharacterLegalFactsQuery) -> CharacterLegalFactsResult:
        reference_at = _as_utc(query.reference_at)
        try:
            character = await resolve_exact_character(self.client, query.character_name)
        except CharacterNotFoundError:
            return _lookup_failure(reference_at, "not_found")
        except CharacterAmbiguousError:
            return _lookup_failure(reference_at, "ambiguous")
        except Exception as error:
            code, retry_after = classify_sarp_failure(error)
            self._log_failure("character", error, code)
            return _lookup_failure(reference_at, code, retry_after)

        selected = frozenset(query.checks)
        serious_task = (
            asyncio.create_task(
                self._prior_serious_convictions(
                    character.id,
                    reference_at=reference_at,
                    exclude_arrest_id=query.exclude_arrest_id,
                    max_matches=query.max_matches_per_check,
                ),
                name="character-prior-serious-convictions",
            )
            if "prior_serious_convictions" in selected
            else None
        )
        fines_task = (
            asyncio.create_task(
                self._overdue_unpaid_fines(
                    character.id,
                    reference_at=reference_at,
                    max_matches=query.max_matches_per_check,
                ),
                name="character-overdue-unpaid-fines",
            )
            if "overdue_unpaid_fines" in selected
            else None
        )
        tasks = tuple(task for task in (serious_task, fines_task) if task is not None)
        if tasks:
            await asyncio.gather(*tasks)
        return CharacterLegalFactsResult(
            character_name=normalize_character_name(character.name),
            reference_at=reference_at,
            prior_serious_convictions=(serious_task.result() if serious_task is not None else None),
            overdue_unpaid_fines=(fines_task.result() if fines_task is not None else None),
        )

    async def _prior_serious_convictions(
        self,
        character_id: int,
        *,
        reference_at: datetime,
        exclude_arrest_id: int | None,
        max_matches: int,
    ) -> CharacterLegalFactResult[PriorSeriousConvictionMatch]:
        matches: list[PriorSeriousConvictionMatch] = []
        records_scanned = 0
        pages_scanned = 0
        uncertain = False
        scan_error: tuple[str, float | None] | None = None
        page = 1
        while page <= MAX_PAGES_PER_CHECK:
            try:
                payload = await self.client.list_arrests(
                    character_id,
                    page=page,
                    limit=PAGE_LIMIT,
                )
            except Exception as error:
                scan_error = classify_sarp_failure(error)
                self._log_failure("prior_serious_convictions", error, scan_error[0])
                break
            pages_scanned += 1
            records_scanned += len(payload.items)
            for arrest in payload.items:
                if arrest.id == exclude_arrest_id:
                    continue
                serious_charges = [
                    charge for charge in arrest.charges if _charge_type_key(charge.type) == "grave"
                ]
                if not serious_charges:
                    continue
                if arrest.created_at is None:
                    uncertain = True
                    continue
                arrested_at = _as_utc(arrest.created_at)
                if arrested_at >= reference_at:
                    continue
                matches.extend(
                    PriorSeriousConvictionMatch(
                        arrest_id=arrest.id,
                        arrested_at=arrested_at,
                        charge_code=charge.code,
                        charge_name=charge.charge,
                        charge_type=charge.type or "GRAVE",
                    )
                    for charge in serious_charges
                )
            total_pages = _total_pages(payload.total, payload.pages)
            if page >= total_pages:
                break
            page += 1
        else:
            uncertain = True

        matches.sort(key=lambda item: item.arrested_at, reverse=True)
        return _fact_result(
            matches,
            records_scanned=records_scanned,
            pages_scanned=pages_scanned,
            max_matches=max_matches,
            uncertain=uncertain,
            scan_error=scan_error,
        )

    async def _overdue_unpaid_fines(
        self,
        character_id: int,
        *,
        reference_at: datetime,
        max_matches: int,
    ) -> CharacterLegalFactResult[OverdueUnpaidFineMatch]:
        matches: list[OverdueUnpaidFineMatch] = []
        records_scanned = 0
        pages_scanned = 0
        uncertain = False
        scan_error: tuple[str, float | None] | None = None
        page = 1
        overdue_delta = timedelta(days=OVERDUE_FINE_DAYS)
        while page <= MAX_PAGES_PER_CHECK:
            try:
                payload = await self.client.list_unpaid_fines(
                    character_id,
                    page=page,
                    limit=PAGE_LIMIT,
                )
            except Exception as error:
                scan_error = classify_sarp_failure(error)
                self._log_failure("overdue_unpaid_fines", error, scan_error[0])
                break
            pages_scanned += 1
            records_scanned += len(payload.items)
            for fine in payload.items:
                if bool(fine.is_paid):
                    continue
                if fine.created_at is None:
                    uncertain = True
                    continue
                issued_at = _as_utc(fine.created_at)
                overdue_since = issued_at + overdue_delta
                if overdue_since > reference_at:
                    continue
                matches.append(
                    OverdueUnpaidFineMatch(
                        fine_id=fine.id,
                        description=fine.fine,
                        amount=fine.cost,
                        issued_at=issued_at,
                        overdue_since=overdue_since,
                        overdue_days_at_reference=int(
                            (reference_at - overdue_since).total_seconds() // 86_400
                        ),
                    )
                )
            total_pages = _total_pages(payload.total, payload.pages)
            if page >= total_pages:
                break
            page += 1
        else:
            uncertain = True

        matches.sort(key=lambda item: item.issued_at, reverse=True)
        return _fact_result(
            matches,
            records_scanned=records_scanned,
            pages_scanned=pages_scanned,
            max_matches=max_matches,
            uncertain=uncertain,
            scan_error=scan_error,
        )

    @staticmethod
    def _log_failure(check: str, error: Exception, failure: str) -> None:
        log_event(
            LOGGER,
            "warning",
            "character_legal_fact_check_failed",
            check=check,
            failure=failure,
            exc_info=error,
            **exception_fields(error),
        )


def _fact_result[MatchT](
    matches: list[MatchT],
    *,
    records_scanned: int,
    pages_scanned: int,
    max_matches: int,
    uncertain: bool,
    scan_error: tuple[str, float | None] | None,
) -> CharacterLegalFactResult[MatchT]:
    match_count = len(matches)
    limited = tuple(matches[:max_matches])
    status: CharacterLegalFactStatus
    if matches:
        status = "confirmed"
        value = True
    elif scan_error is not None and pages_scanned == 0:
        status = "unavailable"
        value = None
    elif scan_error is not None or uncertain:
        status = "incomplete"
        value = None
    else:
        status = "confirmed"
        value = False
    return CharacterLegalFactResult(
        status=status,
        value=value,
        matches=limited,
        match_count=match_count,
        matches_truncated=match_count > len(limited) or uncertain or scan_error is not None,
        records_scanned=records_scanned,
        pages_scanned=pages_scanned,
        error=scan_error[0] if scan_error is not None else None,
        retry_after_seconds=scan_error[1] if scan_error is not None else None,
    )


def _lookup_failure(
    reference_at: datetime,
    error: str,
    retry_after_seconds: float | None = None,
) -> CharacterLegalFactsResult:
    return CharacterLegalFactsResult(
        character_name=None,
        reference_at=reference_at,
        prior_serious_convictions=None,
        overdue_unpaid_fines=None,
        error=error,
        retry_after_seconds=retry_after_seconds,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _charge_type_key(value: str | None) -> str:
    return value.strip().casefold() if value is not None else ""


def _total_pages(total: int, documented_pages: int) -> int:
    calculated = (total + PAGE_LIMIT - 1) // PAGE_LIMIT
    return max(1, documented_pages, calculated)
