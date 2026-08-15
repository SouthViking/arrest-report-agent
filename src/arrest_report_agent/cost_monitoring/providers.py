"""Read-only OpenAI and Railway cost provider clients."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import aiohttp
from openai import APIError, AsyncOpenAI
from openai.types.admin.organization.usage_costs_response import (
    DataResultOrganizationCostsResult,
)

from arrest_report_agent.config.logging import exception_fields, log_event

from .models import OpenAICostSnapshot, RailwayCostSnapshot

LOGGER = logging.getLogger(__name__)

RAILWAY_GRAPHQL_ENDPOINT = "https://backboard.railway.com/graphql/v2"
RAILWAY_USAGE_MEASUREMENTS = (
    "MEMORY_USAGE_GB",
    "CPU_USAGE",
    "NETWORK_TX_GB",
    "DISK_USAGE_GB",
    "BACKUP_USAGE_GB",
)
MINUTES_IN_MONTH = Decimal("43200")
RAILWAY_PRICES = {
    "MEMORY_USAGE_GB": Decimal("10") / MINUTES_IN_MONTH,
    "CPU_USAGE": Decimal("20") / MINUTES_IN_MONTH,
    "NETWORK_TX_GB": Decimal("0.05"),
    "DISK_USAGE_GB": Decimal("0.15") / MINUTES_IN_MONTH,
    "BACKUP_USAGE_GB": Decimal("0.15") / MINUTES_IN_MONTH,
}

RAILWAY_CONTEXT_QUERY = """
query WorkspaceUsageContext($workspaceId: String!) {
  workspace(workspaceId: $workspaceId) {
    customer {
      currentUsage
      billingPeriod { start end }
      usageLimit { hardLimit isOverLimit }
    }
  }
}
"""

RAILWAY_ESTIMATE_QUERY = """
query WorkspaceCostEstimate(
  $workspaceId: String!
  $measurements: [MetricMeasurement!]!
  $startDate: DateTime!
  $endDate: DateTime!
) {
  usage(
    workspaceId: $workspaceId
    measurements: $measurements
    groupBy: [PROJECT_ID]
    startDate: $startDate
    endDate: $endDate
    includeDeleted: true
  ) {
    measurement
    value
  }
  estimatedUsage(
    workspaceId: $workspaceId
    measurements: $measurements
    includeDeleted: true
  ) {
    measurement
    estimatedValue
  }
}
"""


class ProviderResponseError(RuntimeError):
    """A provider returned an invalid or unsuccessful response."""


class OpenAICostProvider:
    """Read monthly spend and an enforced hard limit through the Admin API."""

    def __init__(
        self,
        admin_key: str,
        *,
        project_id: str | None,
        timeout_seconds: float,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self.project_id = project_id
        self.client = client or AsyncOpenAI(
            admin_api_key=admin_key,
            max_retries=1,
            timeout=timeout_seconds,
        )
        self._owns_client = client is None

    async def fetch(self) -> OpenAICostSnapshot:
        now = datetime.now(UTC)
        start = int(datetime(now.year, now.month, 1, tzinfo=UTC).timestamp())
        end = int(now.timestamp()) + 1
        current_spend = await self._monthly_cost(start, end)
        hard_limit = await self._hard_limit()
        remaining = max(Decimal("0"), hard_limit - current_spend) if hard_limit is not None else None
        return OpenAICostSnapshot(
            current_spend=current_spend,
            hard_limit=hard_limit,
            remaining=remaining,
            limit_scope="project" if self.project_id else "organization",
        )

    async def _monthly_cost(self, start: int, end: int) -> Decimal:
        total = Decimal("0")
        cursor: str | None = None
        while True:
            if self.project_id is not None and cursor is not None:
                page = await self.client.admin.organization.usage.costs(
                    start_time=start,
                    end_time=end,
                    limit=31,
                    project_ids=[self.project_id],
                    page=cursor,
                )
            elif self.project_id is not None:
                page = await self.client.admin.organization.usage.costs(
                    start_time=start,
                    end_time=end,
                    limit=31,
                    project_ids=[self.project_id],
                )
            elif cursor is not None:
                page = await self.client.admin.organization.usage.costs(
                    start_time=start,
                    end_time=end,
                    limit=31,
                    page=cursor,
                )
            else:
                page = await self.client.admin.organization.usage.costs(
                    start_time=start,
                    end_time=end,
                    limit=31,
                )
            for bucket in page.data:
                for result in bucket.results:
                    if result.object != "organization.costs.result":
                        continue
                    cost: DataResultOrganizationCostsResult = result
                    if cost.amount is None or cost.amount.value is None:
                        continue
                    if cost.amount.currency and cost.amount.currency.lower() != "usd":
                        continue
                    total += Decimal(str(cost.amount.value))
            if not page.has_more or not page.next_page:
                return total
            cursor = page.next_page

    async def _hard_limit(self) -> Decimal | None:
        try:
            if self.project_id is not None:
                project_limit = await self.client.admin.organization.projects.spend_limit.retrieve(
                    self.project_id
                )
                status = project_limit.enforcement.status
                threshold_amount = project_limit.threshold_amount
            else:
                organization_limit = await self.client.admin.organization.spend_limit.retrieve()
                status = organization_limit.enforcement.status
                threshold_amount = organization_limit.threshold_amount
        except APIError as error:
            log_event(
                LOGGER,
                "warning",
                "openai_spend_limit_unavailable",
                **exception_fields(error),
            )
            return None
        if status != "enforcing":
            return None
        return Decimal(threshold_amount) / Decimal("100")

    async def close(self) -> None:
        if self._owns_client:
            await self.client.close()


class RailwayCostProvider:
    """Read workspace usage using Railway's public GraphQL API."""

    def __init__(
        self,
        api_token: str,
        workspace_id: str,
        *,
        timeout_seconds: float,
        endpoint: str = RAILWAY_GRAPHQL_ENDPOINT,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.workspace_id = workspace_id
        self.endpoint = endpoint
        self._session = session or aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=aiohttp.ClientTimeout(total=timeout_seconds),
        )
        self._owns_session = session is None

    async def fetch(self) -> RailwayCostSnapshot:
        context = await self._graphql(
            RAILWAY_CONTEXT_QUERY,
            {"workspaceId": self.workspace_id},
        )
        workspace = _mapping(context.get("workspace"), "workspace")
        customer = _mapping(workspace.get("customer"), "workspace.customer")
        billing_period = _mapping(customer.get("billingPeriod"), "billingPeriod")
        current_usage = _optional_decimal(customer.get("currentUsage"), "currentUsage")
        hard_limit = self._hard_limit(customer.get("usageLimit"))

        estimated_bill: Decimal | None = None
        try:
            estimate = await self._graphql(
                RAILWAY_ESTIMATE_QUERY,
                {
                    "workspaceId": self.workspace_id,
                    "measurements": list(RAILWAY_USAGE_MEASUREMENTS),
                    "startDate": _required_string(billing_period.get("start"), "billingPeriod.start"),
                    "endDate": _required_string(billing_period.get("end"), "billingPeriod.end"),
                },
            )
            measured_cost = _cost_for_measurements(estimate.get("usage"), value_key="value")
            estimated_cost = _cost_for_measurements(
                estimate.get("estimatedUsage"),
                value_key="estimatedValue",
            )
            adjustment = (
                max(Decimal("0"), current_usage - measured_cost)
                if current_usage is not None
                else Decimal("0")
            )
            estimated_bill = estimated_cost + adjustment
        except (ProviderResponseError, aiohttp.ClientError, TimeoutError) as error:
            log_event(
                LOGGER,
                "warning",
                "railway_cost_estimate_unavailable",
                **exception_fields(error),
            )

        remaining = (
            max(Decimal("0"), hard_limit - current_usage)
            if hard_limit is not None and current_usage is not None
            else None
        )
        return RailwayCostSnapshot(
            current_usage=current_usage,
            estimated_bill=estimated_bill,
            hard_limit=hard_limit,
            remaining=remaining,
        )

    async def _graphql(
        self,
        query: str,
        variables: Mapping[str, object],
    ) -> Mapping[str, object]:
        async with self._session.post(
            self.endpoint,
            json={"query": query, "variables": dict(variables)},
        ) as response:
            if response.status != 200:
                raise ProviderResponseError(f"Railway returned HTTP {response.status}")
            try:
                payload = await response.json()
            except (aiohttp.ContentTypeError, ValueError) as error:
                raise ProviderResponseError("Railway returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise ProviderResponseError("Railway returned an invalid GraphQL payload")
        if payload.get("errors"):
            raise ProviderResponseError("Railway rejected the GraphQL query")
        return _mapping(payload.get("data"), "data")

    @staticmethod
    def _hard_limit(value: object) -> Decimal | None:
        if value is None:
            return None
        limit = _mapping(value, "usageLimit")
        hard_limit = _optional_decimal(limit.get("hardLimit"), "usageLimit.hardLimit")
        return hard_limit if hard_limit is not None and hard_limit > 0 else None

    async def close(self) -> None:
        if self._owns_session:
            await self._session.close()


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ProviderResponseError(f"Missing or invalid {field}")
    return value


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProviderResponseError(f"Missing or invalid {field}")
    return value


def _optional_decimal(value: object, field: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ProviderResponseError(f"Invalid numeric value for {field}")
    try:
        return Decimal(str(value))
    except InvalidOperation as error:
        raise ProviderResponseError(f"Invalid numeric value for {field}") from error


def _cost_for_measurements(value: object, *, value_key: str) -> Decimal:
    if not isinstance(value, list):
        raise ProviderResponseError("Railway returned invalid usage measurements")
    total = Decimal("0")
    for raw_item in value:
        item = _mapping(raw_item, "usage measurement")
        measurement = item.get("measurement")
        if not isinstance(measurement, str) or measurement not in RAILWAY_PRICES:
            continue
        amount = _optional_decimal(item.get(value_key), value_key)
        if amount is not None:
            total += amount * RAILWAY_PRICES[measurement]
    return total
