from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from agents.tool_context import ToolContext

from arrest_report_agent.application.ports import (
    CharacterLegalFactResult,
    CharacterLegalFactsQuery,
    CharacterLegalFactsResult,
    PriorSeriousConvictionMatch,
)
from arrest_report_agent.application.service_context import ReviewRunContext
from arrest_report_agent.application.tools import build_agent_tools
from arrest_report_agent.character_legal_facts import SarpCharacterLegalFactsLookup
from arrest_report_agent.config.persona import PackagedPersonaProvider
from arrest_report_agent.domain import AgentAccessLevel, AgentActivity, InteractionMode, ReviewCase
from arrest_report_agent.infrastructure.sarp import (
    SarpArrest,
    SarpArrestPage,
    SarpCharacterSearchResult,
    SarpFine,
    SarpFinePage,
    SarpPoliceCharge,
    SarpRateLimit,
    SarpRateLimitError,
)
from arrest_report_agent.knowledge import ChargeCatalog, ReportGuide

REFERENCE_AT = datetime(2026, 8, 31, 20, 0, tzinfo=UTC)


class FakeClient:
    def __init__(self) -> None:
        self.characters = [SarpCharacterSearchResult(id=44, name="Terry_McKenzie")]
        self.arrest_pages: dict[int, SarpArrestPage | Exception] = {
            1: SarpArrestPage(items=[], total=0, page=1, limit=100, pages=0)
        }
        self.fine_pages: dict[int, SarpFinePage | Exception] = {
            1: SarpFinePage(items=[], total=0, page=1, limit=100, pages=0)
        }
        self.searches: list[str] = []
        self.arrest_calls: list[tuple[int, int, int]] = []
        self.fine_calls: list[tuple[int, int, int]] = []

    async def search_characters(self, name: str) -> list[SarpCharacterSearchResult]:
        self.searches.append(name)
        return self.characters

    async def list_arrests(
        self,
        character_id: int,
        *,
        page: int = 1,
        limit: int = 3,
    ) -> SarpArrestPage:
        self.arrest_calls.append((character_id, page, limit))
        result = self.arrest_pages[page]
        if isinstance(result, Exception):
            raise result
        return result

    async def list_unpaid_fines(
        self,
        character_id: int,
        *,
        page: int = 1,
        limit: int = 100,
    ) -> SarpFinePage:
        self.fine_calls.append((character_id, page, limit))
        result = self.fine_pages[page]
        if isinstance(result, Exception):
            raise result
        return result


class FakePublisher:
    async def mark_review_started(self) -> None: ...

    async def publish(self, _publication: object) -> None: ...

    async def publish_failure(self, _message: str) -> None: ...


class ActivityRecorder:
    def __init__(self) -> None:
        self.activities: list[AgentActivity] = []

    async def transition(self, activity: AgentActivity) -> None:
        self.activities.append(activity)


class FakeLookup:
    def __init__(self) -> None:
        self.queries: list[CharacterLegalFactsQuery] = []

    async def check(self, query: CharacterLegalFactsQuery) -> CharacterLegalFactsResult:
        self.queries.append(query)
        return CharacterLegalFactsResult(
            character_name="Terry McKenzie",
            reference_at=query.reference_at,
            prior_serious_convictions=(
                CharacterLegalFactResult(
                    status="confirmed",
                    value=True,
                    matches=(
                        PriorSeriousConvictionMatch(
                            arrest_id=300,
                            arrested_at=REFERENCE_AT - timedelta(days=10),
                            charge_code="CP-211",
                            charge_name="Robo",
                            charge_type="GRAVE",
                        ),
                    ),
                    match_count=1,
                    matches_truncated=False,
                    records_scanned=3,
                    pages_scanned=1,
                )
                if "prior_serious_convictions" in query.checks
                else None
            ),
            overdue_unpaid_fines=(
                CharacterLegalFactResult(
                    status="confirmed",
                    value=False,
                    matches=(),
                    match_count=0,
                    matches_truncated=False,
                    records_scanned=2,
                    pages_scanned=1,
                )
                if "overdue_unpaid_fines" in query.checks
                else None
            ),
        )


def _charge(*, charge_id: int, charge_type: str) -> SarpPoliceCharge:
    return SarpPoliceCharge(
        charge_id=charge_id,
        code=f"CP-{charge_id}",
        charge=f"Cargo {charge_id}",
        type=charge_type,
    )


def _arrest(
    arrest_id: int,
    *,
    created_at: datetime | None,
    charge_type: str,
) -> SarpArrest:
    return SarpArrest(
        id=arrest_id,
        suspect_id=44,
        created_at=created_at,
        charges=[_charge(charge_id=arrest_id, charge_type=charge_type)],
    )


def _fine(fine_id: int, *, created_at: datetime | None, paid: bool = False) -> SarpFine:
    return SarpFine(
        id=fine_id,
        character_id=44,
        fine=f"Multa {fine_id}",
        cost=1_500,
        is_paid=paid,
        created_at=created_at,
    )


def _query(
    *checks: str,
    exclude_arrest_id: int | None = None,
    max_matches: int = 5,
) -> CharacterLegalFactsQuery:
    return CharacterLegalFactsQuery(
        character_name="Terry McKenzie",
        checks=checks,  # type: ignore[arg-type]
        reference_at=REFERENCE_AT,
        exclude_arrest_id=exclude_arrest_id,
        max_matches_per_check=max_matches,
    )


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


@pytest.mark.asyncio
async def test_selective_checks_exclude_current_arrest_and_use_historical_reference() -> None:
    client = FakeClient()
    client.arrest_pages[1] = SarpArrestPage(
        items=[
            _arrest(751, created_at=REFERENCE_AT, charge_type="GRAVE"),
            _arrest(300, created_at=REFERENCE_AT - timedelta(days=10), charge_type="GRAVE"),
            _arrest(200, created_at=REFERENCE_AT - timedelta(days=20), charge_type="MENOR"),
            _arrest(800, created_at=REFERENCE_AT + timedelta(days=1), charge_type="GRAVE"),
        ],
        total=4,
        page=1,
        limit=100,
        pages=1,
    )
    lookup = SarpCharacterLegalFactsLookup(client)

    result = await lookup.check(
        _query("prior_serious_convictions", exclude_arrest_id=751)
    )

    assert result.error is None
    assert result.character_name == "Terry McKenzie"
    assert result.prior_serious_convictions is not None
    assert result.prior_serious_convictions.status == "confirmed"
    assert result.prior_serious_convictions.value is True
    assert [item.arrest_id for item in result.prior_serious_convictions.matches] == [300]
    assert client.arrest_calls == [(44, 1, 100)]
    assert client.fine_calls == []
    assert client.searches == ["Terry_McKenzie"]


@pytest.mark.parametrize(
    ("arrest_id", "created_at", "charge_type", "status", "value"),
    [
        (751, REFERENCE_AT - timedelta(days=1), "GRAVE", "confirmed", False),
        (300, REFERENCE_AT + timedelta(days=1), "GRAVE", "confirmed", False),
        (300, REFERENCE_AT - timedelta(days=1), "MENOR", "confirmed", False),
        (300, None, "GRAVE", "incomplete", None),
    ],
    ids=["current-arrest-excluded", "later-arrest", "minor-only", "unknown-date"],
)
@pytest.mark.asyncio
async def test_firearm_prior_check_never_confirms_ineligible_history(
    arrest_id, created_at, charge_type, status, value,
) -> None:
    client = FakeClient()
    client.arrest_pages[1] = SarpArrestPage(
        items=[_arrest(arrest_id, created_at=created_at, charge_type=charge_type)],
        total=1, page=1, limit=100, pages=1,
    )

    result = await SarpCharacterLegalFactsLookup(client).check(
        _query("prior_serious_convictions", exclude_arrest_id=751)
    )

    assert result.prior_serious_convictions is not None
    assert result.prior_serious_convictions.status == status
    assert result.prior_serious_convictions.value is value
    assert result.prior_serious_convictions.matches == ()
    assert client.fine_calls == []


@pytest.mark.asyncio
async def test_overdue_fines_apply_exact_three_day_cutoff_and_skip_paid_rows() -> None:
    client = FakeClient()
    client.fine_pages[1] = SarpFinePage(
        items=[
            _fine(1, created_at=REFERENCE_AT - timedelta(days=3)),
            _fine(2, created_at=REFERENCE_AT - timedelta(days=2, hours=23)),
            _fine(3, created_at=REFERENCE_AT - timedelta(days=10), paid=True),
        ],
        total=3,
        page=1,
        limit=100,
        pages=1,
    )
    lookup = SarpCharacterLegalFactsLookup(client)

    result = await lookup.check(_query("overdue_unpaid_fines"))

    assert result.overdue_unpaid_fines is not None
    assert result.overdue_unpaid_fines.status == "confirmed"
    assert result.overdue_unpaid_fines.value is True
    assert [item.fine_id for item in result.overdue_unpaid_fines.matches] == [1]
    assert result.overdue_unpaid_fines.matches[0].overdue_days_at_reference == 0
    assert client.arrest_calls == []
    assert client.fine_calls == [(44, 1, 100)]


@pytest.mark.asyncio
async def test_both_checks_paginate_and_limit_only_returned_evidence() -> None:
    client = FakeClient()
    client.arrest_pages = {
        1: SarpArrestPage(
            items=[_arrest(3, created_at=REFERENCE_AT - timedelta(days=3), charge_type="GRAVE")],
            total=101,
            page=1,
            limit=100,
            pages=2,
        ),
        2: SarpArrestPage(
            items=[_arrest(2, created_at=REFERENCE_AT - timedelta(days=4), charge_type="GRAVE")],
            total=101,
            page=2,
            limit=100,
            pages=2,
        ),
    }
    client.fine_pages = {
        1: SarpFinePage(
            items=[_fine(3, created_at=REFERENCE_AT - timedelta(days=5))],
            total=101,
            page=1,
            limit=100,
            pages=2,
        ),
        2: SarpFinePage(
            items=[_fine(2, created_at=REFERENCE_AT - timedelta(days=6))],
            total=101,
            page=2,
            limit=100,
            pages=2,
        ),
    }
    lookup = SarpCharacterLegalFactsLookup(client)

    result = await lookup.check(
        _query("prior_serious_convictions", "overdue_unpaid_fines", max_matches=1)
    )

    assert result.prior_serious_convictions is not None
    assert result.prior_serious_convictions.match_count == 2
    assert len(result.prior_serious_convictions.matches) == 1
    assert result.prior_serious_convictions.matches_truncated is True
    assert result.prior_serious_convictions.pages_scanned == 2
    assert result.overdue_unpaid_fines is not None
    assert result.overdue_unpaid_fines.match_count == 2
    assert result.overdue_unpaid_fines.matches_truncated is True
    assert result.overdue_unpaid_fines.pages_scanned == 2


@pytest.mark.asyncio
async def test_missing_dates_and_partial_failures_never_become_confirmed_false() -> None:
    client = FakeClient()
    client.arrest_pages[1] = SarpArrestPage(
        items=[_arrest(1, created_at=None, charge_type="GRAVE")],
        total=1,
        page=1,
        limit=100,
        pages=1,
    )
    client.fine_pages = {
        1: SarpFinePage(items=[], total=101, page=1, limit=100, pages=2),
        2: RuntimeError("temporary failure"),
    }
    lookup = SarpCharacterLegalFactsLookup(client)

    result = await lookup.check(
        _query("prior_serious_convictions", "overdue_unpaid_fines")
    )

    assert result.prior_serious_convictions is not None
    assert result.prior_serious_convictions.status == "incomplete"
    assert result.prior_serious_convictions.value is None
    assert result.overdue_unpaid_fines is not None
    assert result.overdue_unpaid_fines.status == "incomplete"
    assert result.overdue_unpaid_fines.value is None
    assert result.overdue_unpaid_fines.error == "temporarily_unavailable"


@pytest.mark.asyncio
async def test_character_resolution_and_rate_limits_return_safe_structured_failures() -> None:
    client = FakeClient()
    lookup = SarpCharacterLegalFactsLookup(client)
    client.characters = []

    missing = await lookup.check(_query("prior_serious_convictions"))

    assert missing.error == "not_found"
    client.characters = [
        SarpCharacterSearchResult(id=44, name="Terry_McKenzie"),
        SarpCharacterSearchResult(id=45, name="Terry McKenzie"),
    ]
    ambiguous = await lookup.check(_query("prior_serious_convictions"))
    assert ambiguous.error == "ambiguous"

    client.characters = [SarpCharacterSearchResult(id=44, name="Terry_McKenzie")]
    client.arrest_pages[1] = SarpRateLimitError(
        status_code=429,
        message="too_many_requests",
        source="test",
        request_id="request",
        rate_limit=SarpRateLimit(retry_after_seconds=7),
    )
    limited = await lookup.check(_query("prior_serious_convictions"))
    assert limited.error is None
    assert limited.prior_serious_convictions is not None
    assert limited.prior_serious_convictions.status == "unavailable"
    assert limited.prior_serious_convictions.error == "rate_limited"
    assert limited.prior_serious_convictions.retry_after_seconds == 7


@pytest.mark.asyncio
async def test_agent_tool_is_available_to_both_access_levels_and_returns_compact_facts() -> None:
    lookup = FakeLookup()
    full_tools = {
        tool.name: tool  # type: ignore[attr-defined]
        for tool in build_agent_tools(
            ChargeCatalog.load_default(),
            ReportGuide.load_default(),
            access_level=AgentAccessLevel.FULL,
            character_legal_facts_lookup=lookup,
        )
    }
    owner_tools = {
        tool.name: tool  # type: ignore[attr-defined]
        for tool in build_agent_tools(
            ChargeCatalog.load_default(),
            ReportGuide.load_default(),
            access_level=AgentAccessLevel.REPORT_OWNER,
            character_legal_facts_lookup=lookup,
        )
    }
    context = _context()
    activity = ActivityRecorder()
    context.activity = activity
    arguments = json.dumps(
        {
            "character_name": "Terry McKenzie",
            "checks": ["prior_serious_convictions"],
            "reference_at": REFERENCE_AT.isoformat(),
            "exclude_arrest_id": 751,
            "max_matches_per_check": 5,
        }
    )

    result = await full_tools["get_character_legal_facts"].on_invoke_tool(  # type: ignore[attr-defined]
        ToolContext(
            context=context,
            tool_name="get_character_legal_facts",
            tool_call_id="facts",
            tool_arguments=arguments,
        ),
        arguments,
    )

    assert "get_character_legal_facts" in owner_tools
    assert result["ok"] is True
    assert result["checks"]["prior_serious_convictions"]["value"] is True
    assert "overdue_unpaid_fines" not in result["checks"]
    assert lookup.queries[0].exclude_arrest_id == 751
    assert lookup.queries[0].reference_at == REFERENCE_AT
    assert activity.activities == [AgentActivity.RECORDS]


@pytest.mark.asyncio
async def test_prompt_teaches_selective_facts_and_alternative_legal_elements() -> None:
    from arrest_report_agent.application.prompt import build_agent_instructions

    persona = (await PackagedPersonaProvider().load("roy_velazco")).profile
    full = build_agent_instructions(persona, character_legal_facts_enabled=True)
    owner = build_agent_instructions(
        persona,
        AgentAccessLevel.REPORT_OWNER,
        character_legal_facts_enabled=True,
    )

    assert "<sarp_character_legal_facts>" in full
    assert "get_character_legal_facts" in full
    assert "prior_serious_convictions" in full
    assert "overdue_unpaid_fines" in full
    assert "no hallar una multa vencida no descarta una incomparecencia" in full
    assert "no hallar una condena grave no descarta otras condiciones alternativas" in full
    assert "<sarp_character_legal_facts>" in owner
    assert "get_character_legal_facts" in owner
