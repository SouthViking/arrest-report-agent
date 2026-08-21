from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

from arrest_report_agent.cost_monitoring import (
    CostMonitoringService,
    CostMonitoringSnapshot,
    CostMonitoringUnavailableError,
    OpenAICostProvider,
    OpenAICostSnapshot,
    RailwayCostProvider,
    RailwayCostSnapshot,
    build_cost_monitoring_embed,
)


class FakeOpenAIClient:
    def __init__(self) -> None:
        self.cost_calls: list[dict[str, object]] = []
        self.admin = SimpleNamespace(
            organization=SimpleNamespace(
                usage=SimpleNamespace(costs=self.costs),
                projects=SimpleNamespace(spend_limit=SimpleNamespace(retrieve=self.project_spend_limit)),
                spend_limit=SimpleNamespace(retrieve=self.organization_spend_limit),
            )
        )

    async def costs(self, **kwargs: object) -> object:
        self.cost_calls.append(kwargs)
        amount = SimpleNamespace(value=1.84, currency="usd")
        result = SimpleNamespace(object="organization.costs.result", amount=amount)
        bucket = SimpleNamespace(results=[result])
        return SimpleNamespace(data=[bucket], has_more=False, next_page=None)

    async def project_spend_limit(self, project_id: str) -> object:
        assert project_id == "proj_test"
        return SimpleNamespace(
            threshold_amount=1000,
            enforcement=SimpleNamespace(status="enforcing"),
        )

    async def organization_spend_limit(self) -> object:
        raise AssertionError("Project monitoring must use the project limit")


@pytest.mark.asyncio
async def test_openai_provider_reads_monthly_project_spend_and_limit() -> None:
    client = FakeOpenAIClient()
    provider = OpenAICostProvider(
        "admin-secret",
        project_id="proj_test",
        timeout_seconds=5,
        client=client,  # type: ignore[arg-type]
    )

    snapshot = await provider.fetch()

    assert snapshot == OpenAICostSnapshot(
        current_spend=Decimal("1.84"),
        hard_limit=Decimal("10"),
        remaining=Decimal("8.16"),
        limit_scope="project",
    )
    assert client.cost_calls[0]["project_ids"] == ["proj_test"]
    assert isinstance(client.cost_calls[0]["start_time"], int)


@pytest.mark.asyncio
async def test_railway_provider_reads_usage_estimate_and_hard_limit() -> None:
    requests: list[dict[str, object]] = []

    async def graphql(request: web.Request) -> web.Response:
        payload = await request.json()
        requests.append(payload)
        query = payload["query"]
        if "WorkspaceUsageContext" in query:
            return web.json_response(
                {
                    "data": {
                        "workspace": {
                            "customer": {
                                "currentUsage": 3.42,
                                "billingPeriod": {
                                    "start": "2026-08-01T00:00:00Z",
                                    "end": "2026-09-01T00:00:00Z",
                                },
                                "usageLimit": {
                                    "hardLimit": 10,
                                    "isOverLimit": False,
                                },
                            }
                        }
                    }
                }
            )
        return web.json_response(
            {
                "data": {
                    "usage": [{"measurement": "NETWORK_TX_GB", "value": 68.4}],
                    "estimatedUsage": [{"measurement": "NETWORK_TX_GB", "estimatedValue": 98}],
                }
            }
        )

    app = web.Application()
    app.router.add_post("/graphql", graphql)
    async with TestServer(app) as server, TestClient(server) as test_client:
        session = ClientSession()
        try:
            provider = RailwayCostProvider(
                "workspace-token",
                "workspace-1",
                project_id="project-1",
                timeout_seconds=5,
                endpoint=str(test_client.make_url("/graphql")),
                session=session,
            )
            snapshot = await provider.fetch()
        finally:
            await session.close()

    assert snapshot.current_usage == Decimal("3.420")
    assert snapshot.estimated_bill == Decimal("4.900")
    assert snapshot.hard_limit == Decimal("10")
    assert snapshot.remaining == Decimal("6.58")
    assert snapshot.usage_scope == "project"
    assert len(requests) == 2
    assert requests[1]["variables"]["workspaceId"] == "workspace-1"  # type: ignore[index]
    assert requests[1]["variables"]["projectId"] == "project-1"  # type: ignore[index]


class FakeProvider:
    def __init__(self, result: object | Exception) -> None:
        self.result = result
        self.calls = 0

    async def fetch(self) -> object:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_service_caches_partial_provider_results() -> None:
    openai_snapshot = OpenAICostSnapshot(
        current_spend=Decimal("2"),
        hard_limit=None,
        remaining=None,
        limit_scope="organization",
    )
    openai = FakeProvider(openai_snapshot)
    railway = FakeProvider(RuntimeError("temporary failure"))
    service = CostMonitoringService(
        openai=openai,  # type: ignore[arg-type]
        railway=railway,  # type: ignore[arg-type]
        cache_seconds=300,
    )

    first = await service.snapshot()
    second = await service.snapshot()

    assert first is second
    assert first.openai == openai_snapshot
    assert first.railway is None
    assert openai.calls == 1
    assert railway.calls == 1


@pytest.mark.asyncio
async def test_service_rejects_an_empty_result() -> None:
    service = CostMonitoringService(openai=None, railway=None, cache_seconds=300)

    with pytest.raises(CostMonitoringUnavailableError):
        await service.snapshot()


def test_embed_omits_unavailable_fields_and_providers() -> None:
    snapshot = CostMonitoringSnapshot(
        fetched_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        openai=OpenAICostSnapshot(
            current_spend=Decimal("1.84"),
            hard_limit=None,
            remaining=None,
            limit_scope="project",
        ),
        railway=RailwayCostSnapshot(
            current_usage=Decimal("3.42"),
            estimated_bill=None,
            hard_limit=None,
            remaining=None,
        ),
    )

    embed = build_cost_monitoring_embed(snapshot)

    assert embed.title == "💰 Estado de consumo"
    assert [field.name for field in embed.fields] == ["🤖 OpenAI", "🚂 Railway"]
    rendered = "\n".join(field.value for field in embed.fields)
    assert "USD 1,84" in rendered
    assert "USD 3,42" in rendered
    assert "Uso actual del workspace" in rendered
    assert "Mostrando consumo global del workspace" in rendered
    assert "Límite" not in rendered
    assert "estimada" not in rendered
    assert "disponible mediante API" not in rendered


def test_embed_omits_global_usage_note_for_railway_project_scope() -> None:
    snapshot = CostMonitoringSnapshot(
        fetched_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        railway=RailwayCostSnapshot(
            current_usage=Decimal("1.25"),
            estimated_bill=Decimal("2.50"),
            hard_limit=Decimal("10"),
            remaining=Decimal("6.58"),
            usage_scope="project",
        ),
    )

    embed = build_cost_monitoring_embed(snapshot)

    rendered = "\n".join(field.value for field in embed.fields)
    assert "Uso actual del proyecto" in rendered
    assert "Mostrando consumo global" not in rendered
