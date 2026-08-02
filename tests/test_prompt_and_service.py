from __future__ import annotations

import httpx
import openai
import pytest

from arrest_report_agent.application.prompt import build_agent_instructions
from arrest_report_agent.application.service import ReviewService
from arrest_report_agent.application.service_context import ReviewRunContext
from arrest_report_agent.config import BotIdentity
from arrest_report_agent.domain import InteractionMode, ReviewCase
from arrest_report_agent.knowledge import ChargeCatalog, ReportGuide


def test_identity_is_injected_without_prompt_hardcoding() -> None:
    prompt = build_agent_instructions(BotIdentity(display_name="Sgt. Rivera", role_title="Supervisor"))

    assert "Sgt. Rivera" in prompt
    assert "Supervisor" in prompt
    assert "Sgt. Vigil" not in prompt


def test_service_registers_terminal_publish_tool_and_trace_metadata(settings) -> None:
    catalog = ChargeCatalog.load_default()
    guide = ReportGuide.load_default()
    service = ReviewService(settings, catalog, guide)
    tool_names = [tool.name for tool in service.agent.tools]
    case = ReviewCase(
        guild_id=1,
        parent_channel_id=2,
        channel_id=3,
        thread_id=3,
        root_message_id=3,
        trigger_message_id=4,
        requester_user_id=5,
        interaction_mode=InteractionMode.THREAD_FOLLOW_UP,
        messages=[],
        attachments=[],
    )

    metadata = service._trace_metadata(case, attempt=2)

    assert tool_names == [
        "begin_review",
        "get_report_guide",
        "list_charge_index",
        "get_charge_details",
        "publish_review",
    ]
    assert metadata["root_message_id"] == "3"
    assert metadata["trigger_message_id"] == "4"
    assert metadata["user_id"] == "5"
    assert metadata["environment"] == settings.app_environment
    assert metadata["charge_catalog_hash"] == catalog.source_hash


class FailurePublisher:
    def __init__(self) -> None:
        self.failures = []

    async def begin(self, case_subject=None) -> None:
        return None

    async def publish(self, publication) -> None:
        return None

    async def publish_failure(self, kind, public_message, technical_reference) -> None:
        self.failures.append((kind, public_message, technical_reference))


@pytest.mark.asyncio
async def test_service_contains_timeout_and_publishes_contextual_failure(settings, monkeypatch) -> None:
    service = ReviewService(settings, ChargeCatalog.load_default(), ReportGuide.load_default())
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
    publisher = FailurePublisher()
    context = ReviewRunContext(case=case, publisher=publisher, attempt=1)

    async def fail(*args, **kwargs):
        request = httpx.Request("POST", "https://api.openai.test/v1/responses")
        raise openai.APITimeoutError(request=request)

    monkeypatch.setattr("arrest_report_agent.application.service.Runner.run", fail)

    await service.review(
        context,
        [{"role": "user", "content": [{"type": "input_text", "text": "test"}]}],
    )

    assert publisher.failures[0][0].value == "timeout"
    assert "excedió el tiempo" in publisher.failures[0][1]
    assert publisher.failures[0][2] == "3-1-timeout"
