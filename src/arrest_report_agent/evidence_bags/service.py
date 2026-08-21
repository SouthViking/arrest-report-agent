"""Failure-safe application service for live SARP evidence bags."""

from __future__ import annotations

import logging
from typing import Protocol

from arrest_report_agent.application.ports import (
    EvidenceBagCustodyEvent,
    EvidenceBagDetail,
    EvidenceBagItem,
    EvidenceBagLookupResult,
)
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.sarp import (
    SarpError,
    SarpEvidenceDetail,
    SarpNotFoundError,
    SarpRateLimitError,
)

LOGGER = logging.getLogger(__name__)


class EvidenceBagApiClient(Protocol):
    async def get_evidence(self, evidence_id: int) -> SarpEvidenceDetail: ...


class EvidenceBagService:
    """Keep SARP transport and error details outside commands and agent tools."""

    def __init__(self, client: EvidenceBagApiClient) -> None:
        self.client = client

    async def get(self, evidence_id: int) -> EvidenceBagLookupResult:
        if evidence_id <= 0:
            raise ValueError("evidence_id must be positive")
        try:
            payload = await self.client.get_evidence(evidence_id)
        except Exception as error:
            code, retry_after = _safe_failure(error)
            log_event(
                LOGGER,
                "warning",
                "evidence_bag_lookup_failed",
                evidence_id=evidence_id,
                failure=code,
                exc_info=error,
                **exception_fields(error),
            )
            return EvidenceBagLookupResult(
                detail=None,
                error=code,
                retry_after_seconds=retry_after,
            )

        detail = EvidenceBagDetail(
            evidence_id=payload.id,
            items=tuple(
                EvidenceBagItem(
                    analysis_id=item.id,
                    sample_id=item.sample_id,
                    result=item.result.strip(),
                    officer_name=item.police_name.strip(),
                    agency=item.police_agency,
                    examined_at=item.created_at,
                )
                for item in payload.results
            ),
            custody=tuple(
                EvidenceBagCustodyEvent(
                    event_id=entry.id,
                    action=entry.action.strip(),
                    officer_name=entry.police_name.strip(),
                    created_at=entry.created_at,
                )
                for entry in payload.logs
            ),
        )
        log_event(
            LOGGER,
            "info",
            "evidence_bag_lookup_succeeded",
            evidence_id=evidence_id,
            item_count=len(detail.items),
            custody_event_count=len(detail.custody),
        )
        return EvidenceBagLookupResult(detail=detail)


def _safe_failure(error: Exception) -> tuple[str, float | None]:
    if isinstance(error, SarpNotFoundError):
        return "not_found", None
    if isinstance(error, SarpRateLimitError):
        return "rate_limited", error.rate_limit.retry_after_seconds
    if isinstance(error, SarpError):
        return "temporarily_unavailable", None
    return "temporarily_unavailable", None
