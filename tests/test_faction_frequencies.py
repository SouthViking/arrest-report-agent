from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast

from agents.tool_context import ToolContext

from arrest_report_agent.application.ports import (
    FactionFrequencyLookupResult,
    FactionFrequencyProfile,
)
from arrest_report_agent.application.service_context import ReviewRunContext
from arrest_report_agent.application.tools import build_agent_tools
from arrest_report_agent.domain import (
    AgentAccessLevel,
    AgentActivity,
    InteractionMode,
    ReviewCase,
)
from arrest_report_agent.faction_directory import (
    FactionDirectoryService,
    SarpFactionFrequencyLookup,
)
from arrest_report_agent.infrastructure.sarp import (
    SarpFactionDivision,
    SarpFactionFrequency,
    SarpRateLimit,
    SarpRateLimitError,
)
from arrest_report_agent.knowledge import ChargeCatalog, ReportGuide


class FakeFactionClient:
    def __init__(self) -> None:
        self.frequency_calls = 0
        self.division_calls = 0

    async def get_faction_frequencies(self, faction_id: int) -> list[SarpFactionFrequency]:
        assert faction_id == 3
        self.frequency_calls += 1
        await asyncio.sleep(0)
        return [
            SarpFactionFrequency(
                id=57,
                faction=3,
                frequency=488,
                name="SCC_Access",
                division=5,
            ),
            SarpFactionFrequency(
                id=7,
                faction=3,
                frequency=480,
                name="LSC DISP",
                division=0,
            ),
        ]

    async def get_faction_divisions(self, faction_id: int) -> list[SarpFactionDivision]:
        assert faction_id == 3
        self.division_calls += 1
        await asyncio.sleep(0)
        return [SarpFactionDivision(id=5, faction=3, name="Special Enforcement Bureau")]


class DivisionUnavailableClient(FakeFactionClient):
    async def get_faction_divisions(self, faction_id: int) -> list[SarpFactionDivision]:
        assert faction_id == 3
        raise SarpRateLimitError(
            status_code=429,
            message="too_many_requests",
            source="pcu",
            request_id="request-divisions",
            rate_limit=SarpRateLimit(retry_after_seconds=8),
        )


class RateLimitedFrequencyClient(FakeFactionClient):
    async def get_faction_frequencies(self, faction_id: int) -> list[SarpFactionFrequency]:
        assert faction_id == 3
        raise SarpRateLimitError(
            status_code=429,
            message="too_many_requests",
            source="pcu",
            request_id="request-frequencies",
            rate_limit=SarpRateLimit(retry_after_seconds=13),
        )


class FakeFrequencyLookup:
    def __init__(self, result: FactionFrequencyLookupResult) -> None:
        self.result = result
        self.calls = 0

    async def list(self) -> FactionFrequencyLookupResult:
        self.calls += 1
        return self.result


class ActivityRecorder:
    def __init__(self) -> None:
        self.activities: list[AgentActivity] = []

    async def transition(self, activity: AgentActivity) -> None:
        self.activities.append(activity)


def _context(*, access_level: AgentAccessLevel = AgentAccessLevel.FULL) -> ReviewRunContext:
    return ReviewRunContext(
        case=ReviewCase(
            guild_id=1,
            parent_channel_id=2,
            channel_id=3,
            root_message_id=4,
            trigger_message_id=5,
            requester_user_id=6,
            interaction_mode=InteractionMode.INITIAL_REVIEW,
            messages=[],
            attachments=[],
        ),
        publisher=SimpleNamespace(),
        attempt=1,
        access_level=access_level,
    )


def _tools(
    lookup: FakeFrequencyLookup | None,
    access_level: AgentAccessLevel,
) -> dict[str, Any]:
    return {
        cast(Any, tool).name: tool
        for tool in build_agent_tools(
            ChargeCatalog.load_default(),
            ReportGuide.load_default(),
            access_level=access_level,
            faction_frequency_lookup=lookup,
        )
    }


async def test_frequency_lookup_uses_shared_cache_maps_divisions_and_hides_ids() -> None:
    client = FakeFactionClient()
    directory = FactionDirectoryService(
        client,  # type: ignore[arg-type]
        roster_cache_seconds=30,
        rank_cache_seconds=300,
    )
    lookup = SarpFactionFrequencyLookup(directory, faction_id=3)

    first, second = await asyncio.gather(lookup.list(), lookup.list())
    third = await lookup.list()

    expected = (
        FactionFrequencyProfile(
            frequency=480,
            name="LSC DISP",
            division_name=None,
        ),
        FactionFrequencyProfile(
            frequency=488,
            name="SCC Access",
            division_name="Special Enforcement Bureau",
        ),
    )
    assert first.frequencies == second.frequencies == third.frequencies == expected
    assert first.division_catalog_complete is True
    assert client.frequency_calls == 1
    assert client.division_calls == 1
    assert not hasattr(first.frequencies[0], "id")
    assert not hasattr(first.frequencies[0], "faction_id")


async def test_frequency_lookup_preserves_results_when_division_catalog_fails() -> None:
    client = DivisionUnavailableClient()
    result = await SarpFactionFrequencyLookup(
        FactionDirectoryService(client, rank_cache_seconds=300),  # type: ignore[arg-type]
        faction_id=3,
    ).list()

    assert result.error is None
    assert result.division_catalog_complete is False
    assert [item.division_name for item in result.frequencies] == [None, None]


async def test_frequency_lookup_translates_rate_limit_to_safe_state() -> None:
    client = RateLimitedFrequencyClient()
    result = await SarpFactionFrequencyLookup(
        FactionDirectoryService(client, rank_cache_seconds=300),  # type: ignore[arg-type]
        faction_id=3,
    ).list()

    assert result == FactionFrequencyLookupResult(
        frequencies=(),
        division_catalog_complete=False,
        error="rate_limited",
        retry_after_seconds=13,
    )
    assert client.division_calls == 0


async def test_frequency_tool_is_full_access_only_argumentless_and_bounded() -> None:
    lookup = FakeFrequencyLookup(
        FactionFrequencyLookupResult(
            frequencies=(
                FactionFrequencyProfile(480, "LSC DISP", None),
                FactionFrequencyProfile(488, "SCC Access", "SEB"),
            )
        )
    )
    full = _tools(lookup, AgentAccessLevel.FULL)
    owner = _tools(lookup, AgentAccessLevel.REPORT_OWNER)
    disabled = _tools(None, AgentAccessLevel.FULL)
    tool = full["get_faction_frequencies"]
    run_context = _context()
    activity = ActivityRecorder()
    run_context.activity = activity

    assert "get_faction_frequencies" not in owner
    assert "get_faction_frequencies" not in disabled
    assert tool.params_json_schema["properties"] == {}

    responses = []
    for index in range(3):
        responses.append(
            await tool.on_invoke_tool(
                ToolContext(
                    context=run_context,
                    tool_name="get_faction_frequencies",
                    tool_call_id=f"frequency-{index}",
                    tool_arguments="{}",
                ),
                json.dumps({}),
            )
        )

    assert responses[:2] == [
        {
            "ok": True,
            "status": "found",
            "count": 2,
            "division_catalog_complete": True,
            "frequencies": [
                {"frequency": 480, "name": "LSC DISP", "division": None},
                {"frequency": 488, "name": "SCC Access", "division": "SEB"},
            ],
        },
        {
            "ok": True,
            "status": "found",
            "count": 2,
            "division_catalog_complete": True,
            "frequencies": [
                {"frequency": 480, "name": "LSC DISP", "division": None},
                {"frequency": 488, "name": "SCC Access", "division": "SEB"},
            ],
        },
    ]
    assert responses[2] == {"ok": False, "error": "run_frequency_limit_reached"}
    assert lookup.calls == 2
    assert activity.activities == [AgentActivity.FREQUENCIES, AgentActivity.FREQUENCIES]
