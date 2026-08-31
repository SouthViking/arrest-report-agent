from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, date, datetime

import pytest
from agents.tool_context import ToolContext
from alembic import command
from alembic.config import Config

from arrest_report_agent.application.ports import (
    ArrestReportCharge,
    ArrestReportDetail,
    ArrestReportDetailResult,
    ArrestReportSearchQuery,
    ArrestReportSearchResult,
    ArrestReportSummary,
)
from arrest_report_agent.application.service_context import ReviewRunContext
from arrest_report_agent.application.tools import build_agent_tools
from arrest_report_agent.arrest_reports import (
    ArrestReportBindingRepository,
    SarpArrestReportLookup,
)
from arrest_report_agent.domain import AgentAccessLevel, AgentActivity, InteractionMode, ReviewCase
from arrest_report_agent.infrastructure.postgres import PostgresClient, PostgresClientConfig
from arrest_report_agent.infrastructure.postgres.schema import discord_guilds
from arrest_report_agent.infrastructure.sarp import (
    SarpArrestDetail,
    SarpFactionArrestSearchPayload,
    SarpFactionArrestSummary,
    SarpRateLimit,
    SarpRateLimitError,
)
from arrest_report_agent.knowledge import ChargeCatalog, ReportGuide


class FakePublisher:
    async def mark_review_started(self) -> None: ...

    async def publish(self, _publication: object) -> None: ...

    async def publish_failure(self, _message: str) -> None: ...


class ActivityRecorder:
    def __init__(self) -> None:
        self.activities: list[AgentActivity] = []

    async def transition(self, activity: AgentActivity) -> None:
        self.activities.append(activity)


class FakeArrestLookup:
    def __init__(self) -> None:
        self.searches: list[ArrestReportSearchQuery] = []
        self.gets: list[int] = []
        self.binds: list[dict[str, int]] = []
        self.current: list[tuple[int, int]] = []
        self.reports_override: tuple[ArrestReportSummary, ...] | None = None
        self.detail = ArrestReportDetail(
            report_id=321,
            suspect_name="Terry McKenzie",
            officer_name="Charles Harrington",
            description="El sospechoso huyó y posteriormente fue detenido.",
            created_at=datetime(2026, 8, 18, 12, 0, tzinfo=UTC),
            updated_at=datetime(2026, 8, 18, 12, 30, tzinfo=UTC),
            charges=(
                ArrestReportCharge(
                    charge_id=91,
                    code="TEST-91",
                    name="Evasión",
                    charge_type="Felonía",
                    hours=4,
                    fine=500,
                ),
            ),
        )

    async def search(self, query: ArrestReportSearchQuery) -> ArrestReportSearchResult:
        self.searches.append(query)
        reports = self.reports_override
        if reports is None:
            reports = (
                ArrestReportSummary(
                    report_id=321,
                    suspect_name="Terry McKenzie",
                    officer_name="Charles Harrington",
                    description="Narrativa extensa " * 50,
                    created_at=self.detail.created_at,
                ),
            )
        return ArrestReportSearchResult(
            reports=reports,
            total=len(reports),
            page=query.page,
            limit=query.limit,
            has_more=False,
        )

    async def get(self, report_id: int) -> ArrestReportDetailResult:
        self.gets.append(report_id)
        return ArrestReportDetailResult(report=self.detail)

    async def bind(self, **values: int) -> None:
        self.binds.append(values)

    async def get_current(
        self,
        *,
        guild_id: int,
        root_message_id: int,
    ) -> ArrestReportDetailResult:
        self.current.append((guild_id, root_message_id))
        return ArrestReportDetailResult(report=self.detail)


class FakeSarpClient:
    def __init__(self) -> None:
        self.failure: Exception | None = None
        self.get_ids: list[int] = []

    async def search_faction_arrests(
        self,
        _faction_id: int,
        **_filters: object,
    ) -> SarpFactionArrestSearchPayload:
        if self.failure is not None:
            raise self.failure
        return SarpFactionArrestSearchPayload(
            data=[
                SarpFactionArrestSummary(
                    id=1,
                    police_id=2,
                    police_name="Charles_Harrington",
                    suspect_id=3,
                    suspect_name="Terry_McKenzie",
                    description="Reporte",
                    created_at=datetime(2026, 8, 18, tzinfo=UTC),
                ),
                SarpFactionArrestSummary(
                    id=4,
                    police_id=5,
                    police_name="Otro_Agente",
                    suspect_id=6,
                    suspect_name="Terry_McKenzie_Jr",
                    description="Otro reporte",
                    created_at=datetime(2026, 8, 17, tzinfo=UTC),
                ),
            ],
            total=2,
        )

    async def get_arrest(self, report_id: int) -> SarpArrestDetail:
        self.get_ids.append(report_id)
        if self.failure is not None:
            raise self.failure
        return SarpArrestDetail(
            id=report_id,
            police_id=2,
            police_name="Charles_Harrington",
            suspect_id=3,
            suspect_name="Terry_McKenzie",
            description="Reporte vigente",
            created_at=datetime(2026, 8, 18, tzinfo=UTC),
            charges=[],
        )


class FakeBindings:
    def __init__(self) -> None:
        self.report_id: int | None = None

    async def get(self, _guild_id: int, _root_message_id: int) -> object | None:
        if self.report_id is None:
            return None
        return type("Binding", (), {"sarp_arrest_id": self.report_id})()

    async def bind(self, **values: int) -> object:
        self.report_id = values["sarp_arrest_id"]
        return object()


class FailingBindings(FakeBindings):
    async def get(self, _guild_id: int, _root_message_id: int) -> object | None:
        raise RuntimeError("database unavailable")

    async def bind(self, **_values: int) -> object:
        raise RuntimeError("database unavailable")


def _context(*, access_level: AgentAccessLevel = AgentAccessLevel.FULL) -> ReviewRunContext:
    return ReviewRunContext(
        case=ReviewCase(
            guild_id=11111111111111111,
            parent_channel_id=22222222222222222,
            channel_id=33333333333333333,
            thread_id=33333333333333333,
            root_message_id=33333333333333333,
            trigger_message_id=55555555555555555,
            requester_user_id=66666666666666666,
            interaction_mode=InteractionMode.THREAD_FOLLOW_UP,
            messages=[],
            attachments=[],
        ),
        publisher=FakePublisher(),  # type: ignore[arg-type]
        attempt=1,
        access_level=access_level,
    )


def _tools(
    lookup: FakeArrestLookup,
    access_level: AgentAccessLevel,
    *,
    report_channel_ids: frozenset[int] = frozenset({22222222222222222}),
) -> dict[str, object]:
    return {
        tool.name: tool
        for tool in build_agent_tools(
            ChargeCatalog.load_default(),
            ReportGuide.load_default(),
            access_level=access_level,
            arrest_report_lookup=lookup,
            report_channel_ids=report_channel_ids,
        )
    }


def test_arrest_tools_follow_agent_access_level() -> None:
    lookup = FakeArrestLookup()
    full = _tools(lookup, AgentAccessLevel.FULL)
    owner = _tools(lookup, AgentAccessLevel.REPORT_OWNER)

    assert {
        "search_arrest_reports",
        "get_arrest_report",
        "get_current_arrest_report",
    } <= set(full)
    assert "get_current_arrest_report" in owner
    assert "search_arrest_reports" not in owner
    assert "get_arrest_report" not in owner

    disabled = {
        tool.name
        for tool in build_agent_tools(
            ChargeCatalog.load_default(),
            ReportGuide.load_default(),
            access_level=AgentAccessLevel.FULL,
            arrest_report_lookup=None,
        )
    }
    assert not {
        "search_arrest_reports",
        "get_arrest_report",
        "get_current_arrest_report",
    } & disabled


@pytest.mark.asyncio
async def test_full_agent_can_search_fetch_and_replace_thread_binding() -> None:
    lookup = FakeArrestLookup()
    tools = _tools(lookup, AgentAccessLevel.FULL)
    context = _context()
    activity = ActivityRecorder()
    context.activity = activity
    search_args = json.dumps(
        {
            "suspect_name": "Terry McKenzie",
            "date_from": "2026-08-01",
            "page": 1,
            "limit": 5,
            "sort_field": "created_at",
            "sort_order": "desc",
            "exact_suspect": True,
        }
    )
    search = await tools["search_arrest_reports"].on_invoke_tool(  # type: ignore[attr-defined]
        ToolContext(
            context=context,
            tool_name="search_arrest_reports",
            tool_call_id="search",
            tool_arguments=search_args,
        ),
        search_args,
    )
    detail_args = json.dumps({"report_id": 321, "bind_to_current_thread": True})
    detail = await tools["get_arrest_report"].on_invoke_tool(  # type: ignore[attr-defined]
        ToolContext(
            context=context,
            tool_name="get_arrest_report",
            tool_call_id="detail",
            tool_arguments=detail_args,
        ),
        detail_args,
    )

    assert search["ok"] is True
    assert search["status"] == "matched"
    assert search["reports"][0]["report_id"] == 321
    assert len(search["reports"][0]["description_preview"]) <= 400
    assert lookup.searches[0].date_from == date(2026, 8, 1)
    assert detail["bound_to_current_thread"] is True
    assert detail["report"]["charges"][0]["name"] == "Evasión"
    assert lookup.binds == [
        {
            "guild_id": 11111111111111111,
            "root_message_id": 33333333333333333,
            "parent_channel_id": 22222222222222222,
            "thread_id": 33333333333333333,
            "report_id": 321,
            "actor_user_id": 66666666666666666,
            "trigger_message_id": 55555555555555555,
        }
    ]
    assert activity.activities == [AgentActivity.RECORDS, AgentActivity.RECORDS]


@pytest.mark.asyncio
async def test_initial_review_can_bind_the_imminent_report_thread() -> None:
    lookup = FakeArrestLookup()
    tools = _tools(lookup, AgentAccessLevel.FULL)
    context = _context()
    context.case = context.case.model_copy(
        update={
            "channel_id": 22222222222222222,
            "thread_id": None,
            "root_message_id": 55555555555555555,
            "trigger_message_id": 55555555555555555,
            "interaction_mode": InteractionMode.INITIAL_REVIEW,
        }
    )
    arguments = json.dumps({"report_id": 321, "bind_to_current_thread": True})

    result = await tools["get_arrest_report"].on_invoke_tool(  # type: ignore[attr-defined]
        ToolContext(
            context=context,
            tool_name="get_arrest_report",
            tool_call_id="detail",
            tool_arguments=arguments,
        ),
        arguments,
    )

    assert result["bound_to_current_thread"] is True
    assert lookup.binds == [
        {
            "guild_id": 11111111111111111,
            "root_message_id": 55555555555555555,
            "parent_channel_id": 22222222222222222,
            "thread_id": 55555555555555555,
            "report_id": 321,
            "actor_user_id": 66666666666666666,
            "trigger_message_id": 55555555555555555,
        }
    ]


@pytest.mark.asyncio
async def test_owner_can_only_fetch_current_bound_report() -> None:
    lookup = FakeArrestLookup()
    tools = _tools(lookup, AgentAccessLevel.REPORT_OWNER)
    context = _context(access_level=AgentAccessLevel.REPORT_OWNER)
    arguments = "{}"

    result = await tools["get_current_arrest_report"].on_invoke_tool(  # type: ignore[attr-defined]
        ToolContext(
            context=context,
            tool_name="get_current_arrest_report",
            tool_call_id="current",
            tool_arguments=arguments,
        ),
        arguments,
    )

    assert result["ok"] is True
    assert lookup.current == [(11111111111111111, 33333333333333333)]


@pytest.mark.asyncio
async def test_binding_is_rejected_outside_configured_report_channels() -> None:
    lookup = FakeArrestLookup()
    tools = _tools(
        lookup,
        AgentAccessLevel.FULL,
        report_channel_ids=frozenset({99999999999999999}),
    )
    context = _context()
    arguments = json.dumps({"report_id": 321, "bind_to_current_thread": True})

    result = await tools["get_arrest_report"].on_invoke_tool(  # type: ignore[attr-defined]
        ToolContext(
            context=context,
            tool_name="get_arrest_report",
            tool_call_id="detail",
            tool_arguments=arguments,
        ),
        arguments,
    )

    assert result == {"ok": False, "error": "binding_not_allowed"}
    assert lookup.binds == []


@pytest.mark.asyncio
async def test_arrest_tool_call_limits_are_per_run() -> None:
    lookup = FakeArrestLookup()
    tools = _tools(lookup, AgentAccessLevel.FULL)
    context = _context()
    arguments = json.dumps({"limit": 1})

    results = []
    for index in range(4):
        results.append(
            await tools["search_arrest_reports"].on_invoke_tool(  # type: ignore[attr-defined]
                ToolContext(
                    context=context,
                    tool_name="search_arrest_reports",
                    tool_call_id=f"search-{index}",
                    tool_arguments=arguments,
                ),
                arguments,
            )
        )

    assert [result["ok"] for result in results] == [True, True, True, False]
    assert results[-1]["error"] == "run_search_limit_reached"
    assert len(lookup.searches) == 3


@pytest.mark.asyncio
async def test_arrest_search_exposes_not_found_and_ambiguous_states() -> None:
    lookup = FakeArrestLookup()
    tools = _tools(lookup, AgentAccessLevel.FULL)
    context = _context()
    arguments = json.dumps({"suspect_name": "Terry"})

    lookup.reports_override = ()
    missing = await tools["search_arrest_reports"].on_invoke_tool(  # type: ignore[attr-defined]
        ToolContext(
            context=context,
            tool_name="search_arrest_reports",
            tool_call_id="missing",
            tool_arguments=arguments,
        ),
        arguments,
    )
    lookup.reports_override = (
        ArrestReportSummary(
            report_id=321,
            suspect_name="Terry McKenzie",
            officer_name="Charles Harrington",
            description="Uno",
            created_at=lookup.detail.created_at,
        ),
        ArrestReportSummary(
            report_id=322,
            suspect_name="Terry McKenzie Jr",
            officer_name="Marco Ruiz",
            description="Dos",
            created_at=lookup.detail.created_at,
        ),
    )
    ambiguous = await tools["search_arrest_reports"].on_invoke_tool(  # type: ignore[attr-defined]
        ToolContext(
            context=context,
            tool_name="search_arrest_reports",
            tool_call_id="ambiguous",
            tool_arguments=arguments,
        ),
        arguments,
    )

    assert missing["status"] == "not_found"
    assert ambiguous["status"] == "ambiguous"
    assert len(ambiguous["reports"]) == 2


@pytest.mark.asyncio
async def test_lookup_filters_exact_names_and_reads_bound_report_live() -> None:
    client = FakeSarpClient()
    bindings = FakeBindings()
    lookup = SarpArrestReportLookup(client, bindings, faction_id=3)  # type: ignore[arg-type]
    query = ArrestReportSearchQuery(
        suspect_name="terry mckenzie",
        officer_name="charles harrington",
        date_from=None,
        date_to=None,
        page=1,
        limit=10,
        sort_field="created_at",
        sort_order="desc",
        exact_suspect=True,
        exact_officer=True,
    )

    search = await lookup.search(query)
    unbound = await lookup.get_current(guild_id=1, root_message_id=2)
    await lookup.bind(
        guild_id=1,
        root_message_id=2,
        parent_channel_id=3,
        thread_id=4,
        report_id=1,
        actor_user_id=5,
        trigger_message_id=6,
    )
    current = await lookup.get_current(guild_id=1, root_message_id=2)
    current_again = await lookup.get_current(guild_id=1, root_message_id=2)

    assert [report.report_id for report in search.reports] == [1]
    assert unbound.error == "not_bound"
    assert current.report is not None
    assert current.report.description == "Reporte vigente"
    assert current_again.report is not None
    assert client.get_ids == [1, 1]


@pytest.mark.asyncio
async def test_lookup_projects_rate_limits_as_safe_tool_errors() -> None:
    client = FakeSarpClient()
    client.failure = SarpRateLimitError(
        status_code=429,
        message="too_many_requests",
        source="test",
        request_id="request",
        rate_limit=SarpRateLimit(retry_after_seconds=7),
    )
    lookup = SarpArrestReportLookup(client, FakeBindings(), faction_id=3)  # type: ignore[arg-type]

    result = await lookup.get(1)

    assert result.error == "rate_limited"
    assert result.retry_after_seconds == 7


@pytest.mark.asyncio
async def test_lookup_projects_binding_failures_without_breaking_the_agent() -> None:
    lookup = SarpArrestReportLookup(  # type: ignore[arg-type]
        FakeSarpClient(),
        FailingBindings(),
        faction_id=3,
    )

    binding_error = await lookup.bind(
        guild_id=1,
        root_message_id=2,
        parent_channel_id=3,
        thread_id=4,
        report_id=5,
        actor_user_id=6,
        trigger_message_id=7,
    )
    current = await lookup.get_current(guild_id=1, root_message_id=2)

    assert binding_error == "temporarily_unavailable"
    assert current.error == "temporarily_unavailable"


@pytest.mark.asyncio
async def test_binding_repository_replaces_one_root_without_crossing_guilds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("requires TEST_DATABASE_URL with an isolated PostgreSQL database")
    monkeypatch.setenv("DATABASE_URL", database_url)
    await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")
    client = PostgresClient(PostgresClientConfig(url=database_url))
    await client.open()
    guild_ids = (911111111111111111, 922222222222222222)
    try:
        await client.execute(
            discord_guilds.delete().where(discord_guilds.c.discord_id.in_(guild_ids))
        )
        await client.execute(
            discord_guilds.insert().values(
                discord_id=guild_ids[0],
                name="Arrest lookup one",
            )
        )
        await client.execute(
            discord_guilds.insert().values(
                discord_id=guild_ids[1],
                name="Arrest lookup two",
            )
        )
        repository = ArrestReportBindingRepository(client)
        common = {
            "root_message_id": 933333333333333333,
            "parent_channel_id": 944444444444444444,
            "thread_id": 955555555555555555,
            "bound_by_user_id": 966666666666666666,
            "trigger_message_id": 977777777777777777,
        }
        await repository.bind(guild_id=guild_ids[0], sarp_arrest_id=10, **common)
        await repository.bind(guild_id=guild_ids[0], sarp_arrest_id=11, **common)
        await asyncio.gather(
            repository.bind(guild_id=guild_ids[0], sarp_arrest_id=12, **common),
            repository.bind(guild_id=guild_ids[0], sarp_arrest_id=13, **common),
        )

        first = await repository.get(guild_ids[0], common["root_message_id"])
        second = await repository.get(guild_ids[1], common["root_message_id"])

        assert first is not None and first.sarp_arrest_id in {12, 13}
        assert second is None
    finally:
        await client.execute(
            discord_guilds.delete().where(discord_guilds.c.discord_id.in_(guild_ids))
        )
        await client.close()
