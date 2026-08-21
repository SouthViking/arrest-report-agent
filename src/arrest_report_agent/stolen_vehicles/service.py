"""Shared deterministic queries for the feed and Discord command."""

from __future__ import annotations

from arrest_report_agent.infrastructure.sarp import SarpClient, SarpStolenVehiclePage

from .models import StolenVehicleQueryResult, StolenVehicleStatusFilter


class StolenVehicleService:
    def __init__(self, client: SarpClient) -> None:
        self.client = client

    async def list_page(
        self,
        *,
        page: int,
        limit: int,
        status: StolenVehicleStatusFilter,
    ) -> SarpStolenVehiclePage:
        return await self.client.list_stolen_vehicles(
            page=page,
            limit=limit,
            is_active=status.api_state,
        )

    async def list_recent(
        self,
        *,
        count: int,
        status: StolenVehicleStatusFilter,
    ) -> StolenVehicleQueryResult:
        if not 1 <= count <= 50:
            raise ValueError("count must be between 1 and 50")
        page = await self.list_page(page=1, limit=count, status=status)
        ordered = tuple(
            sorted(
                page.items,
                key=lambda item: (item.created_at, item.id),
                reverse=True,
            )
        )
        return StolenVehicleQueryResult(
            items=ordered,
            requested_count=count,
            status=status,
            available_count=page.total,
        )
