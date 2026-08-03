from __future__ import annotations

import json

import pytest
from agents.tool_context import ToolContext

from arrest_report_agent.application.service_context import ReviewRunContext
from arrest_report_agent.application.tools import build_agent_tools
from arrest_report_agent.domain import AgentActivity, InteractionMode, ReviewCase, ReviewVerdict
from arrest_report_agent.knowledge import ChargeCatalog, ReportGuide


class FakePublisher:
    def __init__(self) -> None:
        self.markers = 0
        self.publications = []
        self.messages: list[str] = []

    async def mark_review_started(self) -> None:
        self.markers += 1

    async def publish(self, publication) -> None:
        self.publications.append(publication)

    async def send_message(self, content: str) -> None:
        self.messages.append(content)

    async def publish_failure(self, public_message) -> None:
        raise AssertionError("not expected")


class ActivityRecorder:
    def __init__(self) -> None:
        self.activities: list[AgentActivity] = []

    async def promote(self, activity: AgentActivity) -> None:
        self.activities.append(activity)


@pytest.mark.asyncio
async def test_discord_tools_execute_side_effects_immediately() -> None:
    publisher = FakePublisher()
    case = ReviewCase(
        guild_id=1,
        parent_channel_id=2,
        channel_id=2,
        root_message_id=3,
        trigger_message_id=3,
        requester_user_id=4,
        interaction_mode=InteractionMode.INITIAL_REVIEW,
        messages=[],
        attachments=[],
    )
    run_context = ReviewRunContext(case=case, publisher=publisher, attempt=1)
    tools = {
        tool.name: tool
        for tool in build_agent_tools(ChargeCatalog.load_default(), ReportGuide.load_default())
    }

    publish_arguments = json.dumps(
        {
            "publication": {
                "verdict": "approved",
                "summary": "El expediente es coherente.",
                "findings": [],
                "charges": [],
                "next_step": "Archivar el reporte.",
            }
        }
    )
    publish_context = ToolContext(
        context=run_context,
        tool_name="publish_review",
        tool_call_id="call-publish",
        tool_arguments=publish_arguments,
    )
    await tools["publish_review"].on_invoke_tool(
        publish_context,
        publish_arguments,
    )

    assert publisher.publications[0].verdict is ReviewVerdict.APPROVED
    assert publisher.markers == 0
    assert run_context.published


@pytest.mark.asyncio
async def test_messages_can_be_sent_before_explicit_conversational_completion() -> None:
    publisher = FakePublisher()
    case = ReviewCase(
        guild_id=1,
        parent_channel_id=2,
        channel_id=2,
        root_message_id=3,
        trigger_message_id=3,
        requester_user_id=4,
        interaction_mode=InteractionMode.INITIAL_REVIEW,
        messages=[],
        attachments=[],
    )
    run_context = ReviewRunContext(case=case, publisher=publisher, attempt=1)
    tools = {
        tool.name: tool
        for tool in build_agent_tools(ChargeCatalog.load_default(), ReportGuide.load_default())
    }
    progress_message = "📚 Estoy contrastando ambos cargos, deputy."
    progress_arguments = json.dumps({"content": progress_message})
    tool_context = ToolContext(
        context=run_context,
        tool_name="send_message",
        tool_call_id="call-message",
        tool_arguments=progress_arguments,
    )

    first_result = await tools["send_message"].on_invoke_tool(
        tool_context,
        progress_arguments,
    )
    second_progress = "Ya tengo las definiciones; estoy preparando la diferencia práctica."
    second_arguments = json.dumps({"content": second_progress})
    await tools["send_message"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="send_message",
            tool_call_id="call-message-2",
            tool_arguments=second_arguments,
        ),
        second_arguments,
    )

    assert publisher.messages == [progress_message, second_progress]
    assert first_result == {
        "ok": True,
        "delivery": "discord_message",
        "messages_sent": 1,
        "interaction_complete": False,
    }
    assert run_context.message_sent
    assert run_context.messages_sent_count == 2
    assert not run_context.interaction_completed
    assert not run_context.published

    final_message = "### Diferencia clave\nLa intención específica de matar distingue el intento."
    final_arguments = json.dumps({"content": final_message})
    final_result = await tools["finish_interaction"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="finish_interaction",
            tool_call_id="call-finish",
            tool_arguments=final_arguments,
        ),
        final_arguments,
    )

    assert publisher.messages[-1] == final_message
    assert final_result == {
        "ok": True,
        "delivery": "discord_message",
        "messages_sent": 3,
        "interaction_complete": True,
    }
    assert run_context.messages_sent_count == 3
    assert run_context.interaction_completed


@pytest.mark.asyncio
async def test_knowledge_tools_promote_the_interaction_activity() -> None:
    activity = ActivityRecorder()
    case = ReviewCase(
        guild_id=1,
        parent_channel_id=2,
        channel_id=2,
        root_message_id=3,
        trigger_message_id=3,
        requester_user_id=4,
        interaction_mode=InteractionMode.INITIAL_REVIEW,
        messages=[],
        attachments=[],
    )
    run_context = ReviewRunContext(
        case=case,
        publisher=FakePublisher(),
        attempt=1,
        activity=activity,
    )
    tools = {
        tool.name: tool
        for tool in build_agent_tools(ChargeCatalog.load_default(), ReportGuide.load_default())
    }
    arguments = json.dumps({"charge_ids": [1]})

    await tools["get_charge_details"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="get_charge_details",
            tool_call_id="call-knowledge",
            tool_arguments=arguments,
        ),
        arguments,
    )

    assert activity.activities == [AgentActivity.KNOWLEDGE]
