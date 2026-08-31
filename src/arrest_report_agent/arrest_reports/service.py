"""Application facade for bounded, failure-safe SARP arrest lookups."""

from __future__ import annotations

import logging

from arrest_report_agent.application.ports import (
    ArrestReportCharge,
    ArrestReportDetail,
    ArrestReportDetailResult,
    ArrestReportSearchQuery,
    ArrestReportSearchResult,
    ArrestReportSummary,
)
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.sarp import (
    SarpArrestDetail,
    SarpClient,
    SarpFactionArrestSummary,
)
from arrest_report_agent.infrastructure.sarp.failures import classify_sarp_failure

from .repository import ArrestReportBindingRepository

LOGGER = logging.getLogger(__name__)


class SarpArrestReportLookup:
    """Keep faction scope, transport failures and persistence outside agent tools."""

    def __init__(
        self,
        client: SarpClient,
        bindings: ArrestReportBindingRepository,
        *,
        faction_id: int,
    ) -> None:
        if faction_id <= 0:
            raise ValueError("faction_id must be positive")
        self.client = client
        self.bindings = bindings
        self.faction_id = faction_id

    async def search(self, query: ArrestReportSearchQuery) -> ArrestReportSearchResult:
        try:
            payload = await self.client.search_faction_arrests(
                self.faction_id,
                suspect_name=query.suspect_name,
                officer_name=query.officer_name,
                date_from=query.date_from,
                date_to=query.date_to,
                page=query.page,
                limit=query.limit,
                sort_field=query.sort_field,
                sort_order=query.sort_order,
            )
        except Exception as error:
            return self._search_failure(query, error)
        reports = tuple(
            _summary(item)
            for item in payload.data
            if (
                not query.exact_suspect
                or query.suspect_name is None
                or _name_key(item.suspect_name) == _name_key(query.suspect_name)
            )
            and (
                not query.exact_officer
                or query.officer_name is None
                or _name_key(item.police_name) == _name_key(query.officer_name)
            )
        )
        return ArrestReportSearchResult(
            reports=reports,
            total=payload.total,
            page=query.page,
            limit=query.limit,
            has_more=query.page * query.limit < payload.total,
        )

    async def get(self, report_id: int) -> ArrestReportDetailResult:
        try:
            report = await self.client.get_arrest(report_id)
        except Exception as error:
            return self._detail_failure(error, report_id=report_id)
        return ArrestReportDetailResult(report=_detail(report))

    async def bind(
        self,
        *,
        guild_id: int,
        root_message_id: int,
        parent_channel_id: int,
        thread_id: int,
        report_id: int,
        actor_user_id: int,
        trigger_message_id: int,
    ) -> str | None:
        try:
            await self.bindings.bind(
                guild_id=guild_id,
                root_message_id=root_message_id,
                parent_channel_id=parent_channel_id,
                thread_id=thread_id,
                sarp_arrest_id=report_id,
                bound_by_user_id=actor_user_id,
                trigger_message_id=trigger_message_id,
            )
        except Exception as error:
            log_event(
                LOGGER,
                "warning",
                "report_arrest_binding_save_failed",
                guild_id=guild_id,
                parent_channel_id=parent_channel_id,
                thread_id=thread_id,
                root_message_id=root_message_id,
                sarp_arrest_id=report_id,
                actor_user_id=actor_user_id,
                trigger_message_id=trigger_message_id,
                exc_info=error,
                **exception_fields(error),
            )
            return "temporarily_unavailable"
        log_event(
            LOGGER,
            "info",
            "report_arrest_binding_saved",
            guild_id=guild_id,
            parent_channel_id=parent_channel_id,
            thread_id=thread_id,
            root_message_id=root_message_id,
            sarp_arrest_id=report_id,
            actor_user_id=actor_user_id,
            trigger_message_id=trigger_message_id,
        )
        return None

    async def get_current(
        self,
        *,
        guild_id: int,
        root_message_id: int,
    ) -> ArrestReportDetailResult:
        try:
            binding = await self.bindings.get(guild_id, root_message_id)
        except Exception as error:
            log_event(
                LOGGER,
                "warning",
                "report_arrest_binding_read_failed",
                guild_id=guild_id,
                root_message_id=root_message_id,
                exc_info=error,
                **exception_fields(error),
            )
            return ArrestReportDetailResult(
                report=None,
                error="temporarily_unavailable",
            )
        if binding is None:
            return ArrestReportDetailResult(report=None, error="not_bound")
        return await self.get(binding.sarp_arrest_id)

    def _search_failure(
        self,
        query: ArrestReportSearchQuery,
        error: Exception,
    ) -> ArrestReportSearchResult:
        code, retry_after = classify_sarp_failure(error)
        log_event(
            LOGGER,
            "warning",
            "agent_arrest_search_failed",
            faction_id=self.faction_id,
            page=query.page,
            limit=query.limit,
            failure=code,
            exc_info=error,
            **exception_fields(error),
        )
        return ArrestReportSearchResult(
            reports=(),
            total=0,
            page=query.page,
            limit=query.limit,
            has_more=False,
            error=code,
            retry_after_seconds=retry_after,
        )

    def _detail_failure(self, error: Exception, *, report_id: int) -> ArrestReportDetailResult:
        code, retry_after = classify_sarp_failure(error)
        log_event(
            LOGGER,
            "warning",
            "agent_arrest_detail_failed",
            faction_id=self.faction_id,
            sarp_arrest_id=report_id,
            failure=code,
            exc_info=error,
            **exception_fields(error),
        )
        return ArrestReportDetailResult(
            report=None,
            error=code,
            retry_after_seconds=retry_after,
        )


def _summary(report: SarpFactionArrestSummary) -> ArrestReportSummary:
    return ArrestReportSummary(
        report_id=report.id,
        suspect_name=report.suspect_name,
        officer_name=report.police_name,
        description=report.description,
        created_at=report.created_at,
    )


def _detail(report: SarpArrestDetail) -> ArrestReportDetail:
    return ArrestReportDetail(
        report_id=report.id,
        suspect_name=report.suspect_name,
        officer_name=report.police_name,
        description=report.description,
        created_at=report.created_at,
        updated_at=report.updated_at,
        charges=tuple(
            ArrestReportCharge(
                charge_id=charge.charge_id,
                code=charge.code,
                name=charge.charge,
                charge_type=charge.type,
                hours=charge.hours,
                fine=charge.fine,
            )
            for charge in report.charges
        ),
    )


def _name_key(value: str) -> str:
    return " ".join(value.replace("_", " ").split()).casefold()
