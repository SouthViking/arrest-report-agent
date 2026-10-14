"""Bounded, read-only SARP warrant lookup shared by alerts and approval verification."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from arrest_report_agent.application.ports import ArrestReportDetail
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.sarp import SarpClient, SarpWarrant
from arrest_report_agent.warrant_notifications.service import (
    WarrantChargeDetail,
    WarrantDetail,
    WarrantLookupService,
)

LOGGER = logging.getLogger(__name__)
MAX_PAGES = 3
MAX_DETAILS = 10


def _is_arrest(types: tuple[str, ...]) -> bool:
    return bool({value.casefold() for value in types}.intersection({"arrest", "both"}))


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True)
class ArrestWarrantCheck:
    checked_at: datetime
    status: Literal["confirmed", "incomplete", "unavailable"]
    warrants: tuple[WarrantDetail, ...] = ()
    unavailable_details: tuple[int, ...] = ()

    @property
    def approval_check(self) -> Literal["blocked", "pending", "clear"]:
        if self.warrants:
            return "blocked"
        return "clear" if self.status == "confirmed" else "pending"

    def payload(self, report: ArrestReportDetail) -> dict[str, object]:
        items = []
        for warrant in self.warrants:
            item = warrant.model_dump(mode="json")
            item["description"] = warrant.description[:1500]
            item["charges"] = item["charges"][:20]
            item["description_truncated"] = len(warrant.description) > 1500
            item["charges_truncated"] = len(warrant.charges) > 20
            item["detail_available"] = warrant.warrant_id not in self.unavailable_details
            item["predates_arrest"] = (
                warrant.created_at <= (_utc(report.created_at) or report.created_at)
                if warrant.created_at is not None
                else None
            )
            items.append(item)
        return {
            "report_id": report.report_id,
            "suspect_name": report.suspect_name,
            "arrest_record_created_at": report.created_at.isoformat(),
            "checked_at": self.checked_at.isoformat(),
            "status": self.status,
            "approval_check": self.approval_check,
            "open_arrest_warrants": items,
        }

    def context(self, report: ArrestReportDetail, *, notification_status: str) -> str:
        payload = self.payload(report)
        payload["supervisor_notification"] = notification_status
        return "[SARP_ARREST_WARRANT_CONTEXT]\n" + json.dumps(payload, ensure_ascii=False)


class SarpArrestWarrantLookup:
    def __init__(self, client: SarpClient) -> None:
        self.client = client
        self.lookup = WarrantLookupService(client)

    async def inspect(self, report: ArrestReportDetail) -> ArrestWarrantCheck:
        now = datetime.now(UTC)
        if report.suspect_id is None:
            return ArrestWarrantCheck(now, "unavailable")
        candidates: dict[int, SarpWarrant] = {}
        incomplete = False
        try:
            for page_number in range(1, MAX_PAGES + 1):
                page = await self.client.list_active_warrants(report.suspect_id, page=page_number, limit=100)
                if page.total and not page.items:
                    incomplete = True
                for item in page.items:
                    if item.suspect_id != report.suspect_id:
                        incomplete = True
                        continue
                    if item.active and _is_arrest(item.types):
                        candidates[item.id] = item
                    elif item.active and not item.types:
                        incomplete = True
                if page_number >= page.pages:
                    break
            else:
                incomplete = True
        except Exception as error:
            self._failure("arrest_warrant_list_failed", error, report)
            if not candidates:
                return ArrestWarrantCheck(now, "unavailable")
            incomplete = True
        warrants = []
        unavailable_details = []
        if len(candidates) > MAX_DETAILS:
            incomplete = True
        for candidate in sorted(candidates.values(), key=lambda item: item.id)[:MAX_DETAILS]:
            # The detail may have changed since the list: confirm identity, type and closure again.
            try:
                payload = await self.client.get_warrant(candidate.id)
                if payload.suspect_id != report.suspect_id or payload.id != candidate.id:
                    incomplete = True
                    continue
                if payload.status == 1 and payload.closed_at is None and not payload.types:
                    incomplete = True
                if payload.status != 1 or payload.closed_at is not None or not _is_arrest(payload.types):
                    continue
                detail = self.lookup.project(payload)
                detail = detail.model_copy(
                    update={
                        key: _utc(getattr(detail, key))
                        for key in ("created_at", "updated_at", "signed_at", "closed_at")
                    }
                )
            except Exception as error:
                self._failure("arrest_warrant_detail_failed", error, report)
                incomplete = True
                unavailable_details.append(candidate.id)
                # The list confirmed an open arrest warrant; do not invent the missing signature or issuer.
                detail = WarrantDetail(
                    warrant_id=candidate.id,
                    suspect_name=report.suspect_name,
                    description=candidate.description,
                    types=candidate.types,
                    risk=candidate.risk,
                    status=candidate.status,
                    created_at=_utc(candidate.created_at),
                    charges=tuple(
                        WarrantChargeDetail(
                            code=charge.code,
                            name=charge.charge,
                            severity=charge.type,
                            hours=charge.hours,
                            fine=charge.fine,
                        )
                        for charge in candidate.charges
                    ),
                )
            warrants.append(detail)
        return ArrestWarrantCheck(
            datetime.now(UTC),
            "incomplete" if incomplete else "confirmed",
            tuple(warrants),
            tuple(unavailable_details),
        )

    @staticmethod
    def _failure(event: str, error: Exception, report: ArrestReportDetail) -> None:
        log_event(LOGGER, "warning", event, sarp_arrest_id=report.report_id, **exception_fields(error))
