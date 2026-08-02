from __future__ import annotations

from arrest_report_agent.application.prompt import build_agent_instructions
from arrest_report_agent.application.service import ReviewService
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

