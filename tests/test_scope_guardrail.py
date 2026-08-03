from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from agents import Agent, RunContextWrapper
from agents.usage import Usage

from arrest_report_agent.application.scope import build_scope_guardrail, render_scope_input
from arrest_report_agent.application.service_context import ReviewRunContext
from arrest_report_agent.domain import (
    ConversationMessage,
    DiscordAttachment,
    InteractionMode,
    RequestIntent,
    ReviewCase,
)


def test_scope_input_is_text_only_and_limited_to_current_request() -> None:
    case = ReviewCase(
        guild_id=1,
        parent_channel_id=2,
        channel_id=3,
        thread_id=3,
        root_message_id=3,
        trigger_message_id=5,
        requester_user_id=7,
        interaction_mode=InteractionMode.THREAD_FOLLOW_UP,
        messages=[
            ConversationMessage(
                id=3,
                author_id=7,
                author_display="Deputy",
                author_is_bot=False,
                content="Narrativa extensa que no necesita el clasificador",
                created_at=datetime.now(UTC),
                attachment_ids=[10],
            ),
            ConversationMessage(
                id=5,
                author_id=7,
                author_display="Deputy",
                author_is_bot=False,
                content="¿Dónde encuentro la descripción de ese cargo?",
                created_at=datetime.now(UTC),
                attachment_ids=[],
                is_trigger=True,
            ),
        ],
        attachments=[
            DiscordAttachment(
                id=10,
                message_id=3,
                filename="report.png",
                url="https://cdn.discord.test/private-report.png",
                content_type="image/png",
                size=100,
            )
        ],
    )

    rendered = render_scope_input(case)

    assert "¿Dónde encuentro la descripción de ese cargo?" in rendered
    assert "Está en hilo: sí" in rendered
    assert "Hay contexto previo: sí" in rendered
    assert "Hay material revisable en el contexto: sí" in rendered
    assert "Narrativa extensa" not in rendered
    assert "private-report.png" not in rendered
    assert "https://" not in rendered


@pytest.mark.asyncio
async def test_scope_guardrail_is_blocking_and_aggregates_its_usage(settings, model, monkeypatch) -> None:
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
        publisher=SimpleNamespace(),
        attempt=1,
    )
    wrapper = RunContextWrapper(context=run_context)
    guardrail = build_scope_guardrail(settings, model)
    nested_usage = Usage(requests=1, input_tokens=25, output_tokens=5, total_tokens=30)

    async def classify(*args, **kwargs):
        return SimpleNamespace(
            final_output={
                "allowed": False,
                "category": "unrelated",
                "intent": "out_of_scope",
            },
            context_wrapper=SimpleNamespace(usage=nested_usage),
        )

    monkeypatch.setattr("arrest_report_agent.application.scope.Runner.run", classify)
    result = await guardrail.run(
        Agent(name="Main", instructions="test"),
        "2 + 2",
        wrapper,
    )

    assert guardrail.run_in_parallel is False
    assert result.output.tripwire_triggered
    assert wrapper.usage.total_tokens == 30
    assert run_context.guardrail_usage.total_tokens == 30
    assert run_context.request_intent is RequestIntent.OUT_OF_SCOPE
    assert not run_context.has_revisable_material
