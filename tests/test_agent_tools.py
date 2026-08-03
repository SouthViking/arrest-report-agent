from __future__ import annotations

import json

import pytest
from agents.tool_context import ToolContext

from arrest_report_agent.application.service_context import ReviewRunContext
from arrest_report_agent.application.tools import build_agent_tools
from arrest_report_agent.domain import InteractionMode, ReviewCase, ReviewVerdict
from arrest_report_agent.knowledge import ChargeCatalog, ReportGuide


class FakePublisher:
    def __init__(self) -> None:
        self.begins: list[str | None] = []
        self.publications = []

    async def begin(self, case_subject: str | None = None) -> None:
        self.begins.append(case_subject)

    async def publish(self, publication) -> None:
        self.publications.append(publication)

    async def publish_failure(self, public_message) -> None:
        raise AssertionError("not expected")


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

    begin_arguments = json.dumps({"case_subject": "Terry Parker"})
    begin_context = ToolContext(
        context=run_context,
        tool_name="begin_review",
        tool_call_id="call-begin",
        tool_arguments=begin_arguments,
    )
    await tools["begin_review"].on_invoke_tool(begin_context, begin_arguments)
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

    assert publisher.begins == ["Terry Parker"]
    assert publisher.publications[0].verdict is ReviewVerdict.APPROVED
    assert run_context.started and run_context.published
