from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agents.tool_context import ToolContext

from arrest_report_agent.application.ports import ArrestReportDetail, ArrestReportDetailResult
from arrest_report_agent.application.service_context import ReviewRunContext
from arrest_report_agent.application.tools import build_agent_tools
from arrest_report_agent.arrest_warrants.lookup import ArrestWarrantCheck, SarpArrestWarrantLookup
from arrest_report_agent.arrest_warrants.policy import apply_warrant_policy
from arrest_report_agent.domain import (
    AgentAccessLevel,
    FindingSeverity,
    InteractionMode,
    ReviewCase,
    ReviewFinding,
    ReviewIssueType,
    ReviewPublication,
    ReviewVerdict,
)
from arrest_report_agent.infrastructure.sarp import SarpWarrant, SarpWarrantDetail, SarpWarrantPage
from arrest_report_agent.knowledge import ChargeCatalog, ReportGuide

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
REPORT = ArrestReportDetail(321, "Andy Quintero", "Charles Harrington", "Yo, ...", NOW, None, (), 44)
ROOT = 33333333333333333
CHANNEL = 22222222222222222


def _client(*, open_order=True, signed=True):
    detail = SarpWarrantDetail.model_validate(
        {
            "id": 7,
            "suspect_id": 44,
            "type": ["arrest"],
            "status": 1,
            "suspect_name": "Andy_Quintero",
            "filed_name": "Dennis_Irvan",
            "signed_at": "2026-10-13 12:00:00" if signed else None,
        }
    )
    page = SarpWarrantPage(
        items=[SarpWarrant.model_validate(detail.model_dump())] if open_order else [],
        total=int(open_order),
        page=1,
        pages=1,
        limit=100,
    )
    return SimpleNamespace(
        list_active_warrants=AsyncMock(return_value=page),
        get_warrant=AsyncMock(return_value=detail),
    )


def _run(access=AgentAccessLevel.FULL, **case_changes):
    return ReviewRunContext(
        case=ReviewCase(
            **{
                "guild_id": 11111111111111111,
                "parent_channel_id": CHANNEL,
                "channel_id": ROOT,
                "thread_id": ROOT,
                "root_message_id": ROOT,
                "trigger_message_id": 44444444444444444,
                "requester_user_id": 55555555555555555,
                "structured_report_id": 321,
                "interaction_mode": InteractionMode.INITIAL_REVIEW,
                "messages": [],
                "attachments": [],
                **case_changes,
            }
        ),
        publisher=SimpleNamespace(
            publish=AsyncMock(),
            mark_review_started=AsyncMock(),
            set_message_reaction=AsyncMock(),
        ),
        attempt=1,
        access_level=access,
        preloaded_arrest_report=REPORT,
        report_snapshot_refresher=AsyncMock(return_value=True),
    )


def _tools(client=None, access=AgentAccessLevel.FULL, **kwargs):
    return {
        tool.name: tool
        for tool in build_agent_tools(
            ChargeCatalog.load_default(),
            ReportGuide.load_default(),
            access_level=access,
            arrest_warrant_lookup=SarpArrestWarrantLookup(client) if client is not None else None,
            warrant_check_required=True,
            report_channel_ids=frozenset({CHANNEL}),
            **kwargs,
        )
    }


async def _invoke(tools, run, name, args=None):
    arguments = json.dumps(args or {})
    return await tools[name].on_invoke_tool(
        ToolContext(context=run, tool_name=name, tool_call_id="warrant-test", tool_arguments=arguments),
        arguments,
    )


def _approved(**changes):
    return ReviewPublication(verdict="approved", summary="Aprobado.", **changes)


@pytest.mark.parametrize("signed", [True, False])
@pytest.mark.parametrize("tool", ["approve_arrest_report", "execute_arrest_report_review"])
async def test_open_warrant_blocks_every_full_approval_path(signed, tool):
    client = _client(signed=signed)
    run = _run()
    tools = _tools(client)
    args = (
        {"summary": "Aprobado por el supervisor."}
        if tool == "approve_arrest_report"
        else {
            "publication": _approved().model_dump(mode="json"),
        }
    )
    result = await _invoke(tools, run, tool, args)
    assert result["verdict"] == "corrections_required"
    assert result["warrant_approval_check"] == "blocked"
    assert run.publication.findings[0].severity is FindingSeverity.REQUIRED
    assert run.publication.findings[0].issue_type is ReviewIssueType.OPEN_ARREST_WARRANT
    run.publisher.publish.assert_awaited_once_with(run.publication)
    run.report_snapshot_refresher.assert_awaited_once_with(REPORT, ReviewVerdict.CORRECTIONS_REQUIRED)
    client.list_active_warrants.assert_awaited_once_with(44, page=1, limit=100)


async def test_refresh_clears_historical_context_but_publication_checks_again():
    client = _client(open_order=False)
    tools = _tools(client, AgentAccessLevel.REPORT_OWNER)
    run = _run(AgentAccessLevel.REPORT_OWNER)
    run.arrest_warrant_context = '[SARP_ARREST_WARRANT_CONTEXT]\n{"open_arrest_warrants":[{"warrant_id":7}]}'
    result = await _invoke(tools, run, "get_suspect_arrest_warrants")
    assert result["approval_check"] == "clear"
    # A new/reopened order after the tool call must still block publication.
    client.list_active_warrants.return_value = _client().list_active_warrants.return_value
    result = await _invoke(
        tools,
        run,
        "execute_arrest_report_review",
        {
            "publication": _approved().model_dump(mode="json"),
        },
    )
    assert result["verdict"] == "corrections_required"
    assert client.list_active_warrants.await_count == 2
    # On the next review, confirmed closure permits approval.
    client.list_active_warrants.return_value = _client(open_order=False).list_active_warrants.return_value
    new_run = _run(AgentAccessLevel.REPORT_OWNER)
    result = await _invoke(
        tools,
        new_run,
        "execute_arrest_report_review",
        {
            "publication": _approved().model_dump(mode="json"),
        },
    )
    assert result["verdict"] == "approved"
    assert new_run.publication.findings == []


@pytest.mark.parametrize("failure", ["sarp_down", "client_disabled", "missing_identity", "incomplete"])
async def test_failed_verification_keeps_approval_pending_without_a_false_order(failure):
    client = _client(open_order=False)
    run = _run()
    if failure == "sarp_down":
        client.list_active_warrants.side_effect = RuntimeError("SARP unavailable")
    elif failure == "client_disabled":
        client = None
    elif failure == "missing_identity":
        run.latest_arrest_report = replace(REPORT, suspect_id=None)
    else:
        client.list_active_warrants.return_value = SarpWarrantPage(
            items=[], total=1, page=1, pages=1, limit=100
        )
    result = await _invoke(_tools(client), run, "approve_arrest_report", {"summary": "Aprobado."})
    assert result["verdict"] == "inconclusive"
    assert result["warrant_approval_check"] == "pending"
    assert run.publication.findings == []
    assert "SARP" in run.publication.manual_review_reason
    run.publisher.publish.assert_awaited_once()


async def test_missing_detail_blocks_using_confirmed_listing_without_inventing_issuer():
    client = _client()
    client.get_warrant.side_effect = RuntimeError("SARP detail unavailable")
    run = _run(AgentAccessLevel.REPORT_OWNER)
    tools = _tools(client, AgentAccessLevel.REPORT_OWNER)
    result = await _invoke(tools, run, "get_suspect_arrest_warrants")
    assert result["approval_check"] == "blocked"
    assert result["open_arrest_warrants"][0]["filed_name"] is None
    assert result["open_arrest_warrants"][0]["detail_available"] is False
    result = await _invoke(
        tools,
        run,
        "execute_arrest_report_review",
        {
            "publication": _approved().model_dump(mode="json"),
        },
    )
    assert result["verdict"] == "corrections_required"


async def test_refresh_never_uses_another_loaded_report_for_structured_case():
    client = _client(open_order=False)
    report_lookup = SimpleNamespace(get=AsyncMock(return_value=ArrestReportDetailResult(REPORT)))
    run = _run(AgentAccessLevel.REPORT_OWNER)
    run.latest_arrest_report = replace(REPORT, report_id=999, suspect_id=99)
    result = await _invoke(
        _tools(client, AgentAccessLevel.REPORT_OWNER, arrest_report_lookup=report_lookup),
        run,
        "get_suspect_arrest_warrants",
    )
    assert result["report_id"] == 321
    assert run.latest_arrest_report == REPORT
    report_lookup.get.assert_awaited_once_with(321)
    client.list_active_warrants.assert_awaited_once_with(44, page=1, limit=100)


async def test_unstructured_owner_uses_exact_root_binding_instead_of_a_loaded_candidate():
    client = _client(open_order=False)
    lookup = SimpleNamespace(get_current=AsyncMock(return_value=ArrestReportDetailResult(REPORT)))
    run = _run(AgentAccessLevel.REPORT_OWNER, structured_report_id=None)
    run.latest_arrest_report = replace(REPORT, report_id=999, suspect_id=99)
    result = await _invoke(
        _tools(client, AgentAccessLevel.REPORT_OWNER, arrest_report_lookup=lookup),
        run,
        "get_suspect_arrest_warrants",
    )
    assert result["approval_check"] == "clear"
    lookup.get_current.assert_awaited_once_with(guild_id=run.case.guild_id, root_message_id=ROOT)
    client.list_active_warrants.assert_awaited_once_with(44, page=1, limit=100)


@pytest.mark.parametrize(
    "available,open_order,expected", [(True, True, False), (False, False, False), (True, False, True)]
)
async def test_root_approval_reaction_obeys_same_clearance(available, open_order, expected):
    client = _client(open_order=open_order) if available else None
    run = _run()
    result = await _invoke(
        _tools(client), run, "set_message_reaction", {"message_id": str(ROOT), "emoji": "✅"}
    )
    assert result["ok"] is expected
    assert run.publisher.set_message_reaction.await_count == int(expected)


async def test_partial_scope_does_not_become_full_report_approval():
    client = _client()
    run = _run()
    result = await _invoke(
        _tools(client),
        run,
        "execute_arrest_report_review",
        {
            "publication": _approved(review_scope="narrative").model_dump(mode="json"),
        },
    )
    assert result["verdict"] == "approved" and result["review_scope"] == "narrative"
    client.list_active_warrants.assert_not_awaited()
    run.report_snapshot_refresher.assert_not_awaited()


async def test_unknown_detail_type_does_not_confirm_absence():
    client = _client()
    client.get_warrant.return_value = client.get_warrant.return_value.model_copy(update={"types": ()})
    check = await SarpArrestWarrantLookup(client).inspect(REPORT)
    assert check.approval_check == "pending"


def test_clearance_preserves_other_required_corrections_and_finding_capacity():
    other = ReviewFinding(
        issue_type=ReviewIssueType.ORTHOGRAPHY,
        severity="required",
        title="Corrección real",
        detail="Falta corregir el texto.",
        recommendation="Corrige el texto.",
    )
    publication = ReviewPublication(verdict="corrections_required", summary="Rechazado.", findings=[other])
    clear = ArrestWarrantCheck(NOW, "confirmed")
    assert apply_warrant_policy(publication, clear).verdict is ReviewVerdict.CORRECTIONS_REQUIRED
    pending = apply_warrant_policy(publication, ArrestWarrantCheck(NOW, "unavailable"))
    assert pending.findings == [other] and pending.verdict is ReviewVerdict.CORRECTIONS_REQUIRED
    assert "SARP" in pending.manual_review_reason


async def test_warrant_finding_limit_does_not_drop_an_unresolved_correction_silently():
    check = await SarpArrestWarrantLookup(_client()).inspect(REPORT)
    findings = [
        ReviewFinding(
            issue_type=ReviewIssueType.ORTHOGRAPHY,
            severity="required",
            title=f"Corrección {n}",
            detail="Deficiencia probada.",
            recommendation="Corrige el texto.",
        )
        for n in range(12)
    ]
    publication = ReviewPublication(verdict="corrections_required", summary="Rechazado.", findings=findings)
    gated = apply_warrant_policy(publication, check)
    assert len(gated.findings) == 12
    assert gated.findings[0].issue_type is ReviewIssueType.OPEN_ARREST_WARRANT
    assert "Corrección 11" in gated.manual_review_reason


async def test_tool_budget_cannot_skip_the_final_approval_check():
    client = _client(open_order=False)
    run = _run()
    tools = _tools(client)
    for _ in range(3):
        assert (await _invoke(tools, run, "get_suspect_arrest_warrants"))["approval_check"] == "clear"
    assert (await _invoke(tools, run, "get_suspect_arrest_warrants"))["error"] == "run_warrant_limit_reached"
    client.list_active_warrants.return_value = _client().list_active_warrants.return_value
    result = await _invoke(tools, run, "approve_arrest_report", {"summary": "Aprobado."})
    assert result["verdict"] == "corrections_required"
    assert client.list_active_warrants.await_count == 4


async def test_closed_warrant_removes_stale_finding_without_inventing_report_approval():
    open_check = await SarpArrestWarrantLookup(_client()).inspect(REPORT)
    stale = apply_warrant_policy(_approved(), open_check)
    closed = apply_warrant_policy(stale, ArrestWarrantCheck(NOW, "confirmed"))
    assert closed.findings == []
    assert closed.verdict is ReviewVerdict.INCONCLUSIVE
