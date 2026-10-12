from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agents import RunContextWrapper
from agents.exceptions import ModelBehaviorError
from agents.tool_context import ToolContext

from arrest_report_agent.application.service_context import ReviewRunContext
from arrest_report_agent.application.tool_errors import (
    MAX_TOOL_ARGUMENT_RECOVERIES,
    recover_tool_argument_error,
)
from arrest_report_agent.application.tools import build_agent_tools
from arrest_report_agent.config.logging import STRUCTURED_EVENT_ATTRIBUTE
from arrest_report_agent.domain import AgentAccessLevel, InteractionMode, ReviewCase
from arrest_report_agent.domain.errors import PublicationError
from arrest_report_agent.knowledge import ChargeCatalog, ReportGuide

TOOL_NAME = "execute_arrest_report_review"
INVALID_INPUT_ERROR = f"Invalid JSON input for tool {TOOL_NAME}"


@pytest.fixture
def run_context():
    return ReviewRunContext(
        case=ReviewCase(
            guild_id=10,
            parent_channel_id=19,
            channel_id=20,
            thread_id=20,
            root_message_id=1,
            trigger_message_id=2,
            requester_user_id=30,
            interaction_mode=InteractionMode.THREAD_FOLLOW_UP,
            messages=[],
            attachments=[],
        ),
        publisher=SimpleNamespace(
            mark_review_started=AsyncMock(),
            publish=AsyncMock(),
            publish_failure=AsyncMock(),
            send_message=AsyncMock(),
        ),
        attempt=1,
    )


@pytest.fixture
def tools():
    return {
        tool.name: tool
        for tool in build_agent_tools(ChargeCatalog.load_default(), ReportGuide.load_default())
    }


@pytest.fixture
def valid_arguments():
    return json.dumps(
        {
            "publication": {
                "review_scope": "full",
                "focus": None,
                "verdict": "approved",
                "summary": "El expediente es coherente.",
                "findings": [],
                "charges": [],
                "manual_review_reason": None,
            }
        }
    )


async def invoke(tool, context, arguments, *, call_id="call-review"):
    return await tool.on_invoke_tool(
        ToolContext(
            context=context,
            tool_name=tool.name,
            tool_call_id=call_id,
            tool_arguments=arguments,
        ),
        arguments,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        '{"publication":',
        "[]",
        "{}",
        '{"publication":42}',
        '{"publication":{"verdict":"unknown","summary":"Reporte revisado."}}',
        '{"publication":{"verdict":"approved","summary":""}}',
        '{"publication":{"review_scope":"targeted","verdict":"approved","summary":"Revisado."}}',
    ],
)
async def test_sdk_argument_errors_return_repair_instructions_without_side_effects(
    run_context, tools, arguments
):
    tool = tools[TOOL_NAME]
    result = json.loads(await invoke(tool, run_context, arguments))

    assert result["ok"] is False
    assert result["error"] == "invalid_tool_arguments"
    assert result["tool_name"] == TOOL_NAME
    assert result["retryable"] is True
    assert result["recovery_attempt"] == 1
    assert result["remaining_recoveries"] == MAX_TOOL_ARGUMENT_RECOVERIES - 1
    assert "not executed" in result["instruction"]
    assert "call this tool again" in result["instruction"]
    assert tool.strict_json_schema is True
    assert run_context.tool_argument_recovery_counts == {TOOL_NAME: 1}
    assert run_context.started_tool_call_ids == set()
    assert not run_context.published
    assert not run_context.tool_argument_recovery_exhausted
    run_context.publisher.mark_review_started.assert_not_called()
    run_context.publisher.publish.assert_not_called()
    run_context.publisher.publish_failure.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("access_level", [AgentAccessLevel.FULL, AgentAccessLevel.REPORT_OWNER])
async def test_corrected_arguments_publish_once_after_two_recoveries(
    run_context, valid_arguments, access_level
):
    run_context.access_level = access_level
    tool = next(
        tool
        for tool in build_agent_tools(
            ChargeCatalog.load_default(), ReportGuide.load_default(), access_level=access_level
        )
        if tool.name == TOOL_NAME
    )
    for index in range(MAX_TOOL_ARGUMENT_RECOVERIES):
        result = json.loads(await invoke(tool, run_context, '{"publication":', call_id=f"bad-{index}"))
        assert result["recovery_attempt"] == index + 1

    result = await invoke(tool, run_context, valid_arguments, call_id="corrected")
    repeated = await invoke(tool, run_context, valid_arguments, call_id="already-published")

    assert result == {"ok": True, "review_scope": "full", "verdict": "approved"}
    assert repeated == {"ok": True, "already_published": True}
    assert run_context.published
    run_context.publisher.mark_review_started.assert_awaited_once()
    run_context.publisher.publish.assert_awaited_once()
    run_context.publisher.publish_failure.assert_not_called()


@pytest.mark.asyncio
async def test_argument_recovery_limit_is_per_run_and_fails_closed(run_context, tools):
    tool = tools[TOOL_NAME]
    for index in range(MAX_TOOL_ARGUMENT_RECOVERIES):
        await invoke(tool, run_context, '{"publication":', call_id=f"bad-{index}")

    with pytest.raises(ModelBehaviorError, match="Invalid JSON input"):
        await invoke(tool, run_context, '{"publication":', call_id="exhausted")

    assert run_context.tool_argument_recovery_counts[TOOL_NAME] == MAX_TOOL_ARGUMENT_RECOVERIES + 1
    assert run_context.tool_argument_recovery_exhausted
    run_context.publisher.publish.assert_not_called()
    fresh_context = ReviewRunContext(case=run_context.case, publisher=run_context.publisher, attempt=2)
    result = json.loads(await invoke(tool, fresh_context, '{"publication":', call_id="fresh-run"))
    assert result["recovery_attempt"] == 1
    assert fresh_context.started_tool_call_ids == set()
    assert not fresh_context.tool_argument_recovery_exhausted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        PublicationError("Discord unavailable"),
        RuntimeError("Database unavailable"),
        ModelBehaviorError("Internal invariant failed"),
        ModelBehaviorError(INVALID_INPUT_ERROR),
    ],
)
async def test_internal_errors_propagate_even_with_the_sdk_argument_error_message(
    run_context, tools, valid_arguments, error
):
    run_context.publisher.publish.side_effect = error

    with pytest.raises(type(error)) as caught:
        await invoke(tools[TOOL_NAME], run_context, valid_arguments)

    assert caught.value is error
    assert run_context.tool_argument_recovery_counts == {}
    assert run_context.started_tool_call_ids == {"call-review"}
    run_context.publisher.publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_failure_after_publication_is_not_converted_to_an_argument_retry(
    run_context, tools, valid_arguments
):
    error = ModelBehaviorError(INVALID_INPUT_ERROR)
    run_context.report_snapshot_refresher = AsyncMock(side_effect=error)

    with pytest.raises(ModelBehaviorError) as caught:
        await invoke(tools[TOOL_NAME], run_context, valid_arguments)

    assert caught.value is error
    assert run_context.tool_argument_recovery_counts == {}
    run_context.publisher.publish.assert_awaited_once()
    run_context.report_snapshot_refresher.assert_awaited_once()


@pytest.mark.asyncio
async def test_recovery_logs_include_case_and_limit_without_raw_arguments(run_context, tools, caplog):
    arguments = '{"publication":{"summary":"PRIVATE_REPORT_DATA'
    with caplog.at_level("WARNING", logger="arrest_report_agent.application.tool_errors"):
        for index in range(MAX_TOOL_ARGUMENT_RECOVERIES):
            output = await invoke(tools[TOOL_NAME], run_context, arguments, call_id=f"bad-{index}")
            assert "PRIVATE_REPORT_DATA" not in output
        with pytest.raises(ModelBehaviorError):
            await invoke(tools[TOOL_NAME], run_context, arguments, call_id="exhausted")

    events = [
        getattr(record, STRUCTURED_EVENT_ATTRIBUTE)
        for record in caplog.records
        if hasattr(record, STRUCTURED_EVENT_ATTRIBUTE)
    ]
    assert [event["event"] for event in events] == [
        "tool_arguments_recovery_requested",
        "tool_arguments_recovery_requested",
        "tool_arguments_recovery_exhausted",
    ]
    assert events[0]["root_message_id"] == run_context.case.root_message_id
    assert events[0]["thread_id"] == run_context.case.thread_id
    assert events[0]["attempt"] == run_context.attempt
    assert events[0]["tool_name"] == TOOL_NAME
    assert events[-1]["max_recoveries"] == MAX_TOOL_ARGUMENT_RECOVERIES
    assert "PRIVATE_REPORT_DATA" not in json.dumps(events)
    assert "PRIVATE_REPORT_DATA" not in caplog.text


def test_callback_without_sdk_tool_context_does_not_swallow_the_error(run_context):
    error = ModelBehaviorError(INVALID_INPUT_ERROR)
    with pytest.raises(ModelBehaviorError) as caught:
        recover_tool_argument_error(RunContextWrapper(context=run_context), error)
    assert caught.value is error
    assert run_context.tool_argument_recovery_counts == {}


@pytest.mark.asyncio
async def test_other_publication_tools_keep_their_existing_error_policy(run_context, tools):
    with pytest.raises(ModelBehaviorError):
        await invoke(tools["send_message"], run_context, '{"content":')
    assert run_context.tool_argument_recovery_counts == {}
    run_context.publisher.send_message.assert_not_called()
