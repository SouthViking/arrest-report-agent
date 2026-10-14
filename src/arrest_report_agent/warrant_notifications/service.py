"""Failure-safe application service for live SARP warrant details."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.sarp import (
    SarpError,
    SarpNotFoundError,
    SarpRateLimitError,
    SarpWarrantDetail,
)

LOGGER = logging.getLogger(__name__)
_PLACEHOLDER_NAMES = frozenset({"firstname lastname", "null", "none", "n/a"})
_SAMP_COLOR_TAG = re.compile(r"\{[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?\}")


class WarrantChargeDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str | None = None
    name: str = Field(min_length=1)
    severity: str | None = None
    hours: int | float | None = None
    fine: int | float | None = None


class WarrantDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    warrant_id: int = Field(gt=0)
    suspect_name: str = Field(min_length=1)
    description: str
    types: tuple[str, ...]
    risk: str | None = None
    status: int
    filed_name: str | None = None
    signed_at: datetime | None = None
    signed_name: str | None = None
    closed_at: datetime | None = None
    closed_name: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    charges: tuple[WarrantChargeDetail, ...] = ()


WarrantLookupError = Literal["not_found", "rate_limited", "temporarily_unavailable"]


class WarrantLookupResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    detail: WarrantDetail | None = None
    error: WarrantLookupError | None = None
    retry_after_seconds: float | None = None


class WarrantApiClient(Protocol):
    async def get_warrant(self, warrant_id: int) -> SarpWarrantDetail: ...


class WarrantLookupService:
    """Keep SARP transport errors and placeholder cleanup outside Discord UI."""

    def __init__(self, client: WarrantApiClient) -> None:
        self.client = client

    async def get(self, warrant_id: int) -> WarrantLookupResult:
        if warrant_id <= 0:
            raise ValueError("warrant_id must be positive")
        try:
            payload = await self.client.get_warrant(warrant_id)
        except Exception as error:
            code, retry_after = _safe_failure(error)
            log_event(
                LOGGER,
                "warning",
                "warrant_lookup_failed",
                warrant_id=warrant_id,
                failure=code,
                exc_info=error,
                **exception_fields(error),
            )
            return WarrantLookupResult(
                error=code,
                retry_after_seconds=retry_after,
            )

        detail = self.project(payload)
        log_event(
            LOGGER,
            "info",
            "warrant_lookup_succeeded",
            warrant_id=warrant_id,
            charge_count=len(detail.charges),
        )
        return WarrantLookupResult(detail=detail)

    @staticmethod
    def project(payload: SarpWarrantDetail) -> WarrantDetail:
        """Reuse the same IC-safe projection for notifications and review context."""
        return WarrantDetail(
            warrant_id=payload.id,
            suspect_name=_display_name(payload.suspect_name) or "Sin identificar",
            description=_clean_text(payload.description),
            types=tuple(_clean_text(value).casefold() for value in payload.types if value.strip()),
            risk=_clean_text(payload.risk) or None,
            status=payload.status,
            filed_name=_display_name(payload.filed_name),
            signed_at=payload.signed_at,
            signed_name=_display_name(payload.signed_name),
            closed_at=payload.closed_at,
            closed_name=_display_name(payload.closed_name),
            created_at=payload.created_at,
            updated_at=payload.updated_at,
            charges=tuple(
                WarrantChargeDetail(
                    code=_clean_text(charge.code) or None,
                    name=_clean_text(charge.charge) or "Cargo sin nombre",
                    severity=_clean_text(charge.type) or None,
                    hours=charge.hours,
                    fine=charge.fine,
                )
                for charge in payload.charges
            ),
        )


def _clean_text(value: str | None) -> str:
    if value is None:
        return ""
    return _SAMP_COLOR_TAG.sub("", value).strip()


def _display_name(value: str | None) -> str | None:
    cleaned = " ".join(_clean_text(value).replace("_", " ").split())
    if not cleaned or cleaned.casefold() in _PLACEHOLDER_NAMES:
        return None
    return cleaned


def _safe_failure(error: Exception) -> tuple[WarrantLookupError, float | None]:
    if isinstance(error, SarpNotFoundError):
        return "not_found", None
    if isinstance(error, SarpRateLimitError):
        return "rate_limited", error.rate_limit.retry_after_seconds
    if isinstance(error, SarpError):
        return "temporarily_unavailable", None
    return "temporarily_unavailable", None
