from __future__ import annotations

from types import SimpleNamespace

import pytest
from agents import Agent

from arrest_report_agent.application.hooks import ReviewLifecycleHooks
from arrest_report_agent.application.service_context import ReviewRunContext
from arrest_report_agent.domain import AgentActivity, InteractionMode, RequestIntent, ReviewCase


class HookPublisher:
    def __init__(self) -> None:
        self.markers = 0

    async def mark_review_started(self) -> None:
        self.markers += 1


class ActivityRecorder:
    def __init__(self) -> None:
        self.activities: list[AgentActivity] = []

    async def transition(self, activity: AgentActivity) -> None:
        self.activities.append(activity)


def run_context(intent: RequestIntent, *, has_material: bool) -> ReviewRunContext:
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
    return ReviewRunContext(
        case=case,
        publisher=HookPublisher(),
        attempt=1,
        request_intent=intent,
        has_revisable_material=has_material,
        activity=ActivityRecorder(),
    )


@pytest.mark.asyncio
async def test_review_hook_marks_ready_review_once_and_tracks_lifecycle() -> None:
    context = run_context(RequestIntent.REVIEW, has_material=True)
    hook_context = SimpleNamespace(context=context)
    agent = Agent(name="Main", instructions="test")
    hooks = ReviewLifecycleHooks()

    await hooks.on_agent_start(hook_context, agent)
    await hooks.on_agent_start(hook_context, agent)
    await hooks.on_agent_end(hook_context, agent, {"ok": True})

    assert context.publisher.markers == 1
    assert context.agent_started
    assert context.agent_completed
    assert context.review_marked
    assert isinstance(context.activity, ActivityRecorder)
    assert context.activity.activities == [AgentActivity.REVIEW]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("intent", "has_material"),
    [
        (RequestIntent.CONVERSATION, True),
        (RequestIntent.REVIEW, False),
    ],
)
async def test_hook_does_not_mark_conversation_or_materialless_review(
    intent: RequestIntent,
    has_material: bool,
) -> None:
    context = run_context(intent, has_material=has_material)

    await ReviewLifecycleHooks().on_agent_start(
        SimpleNamespace(context=context),
        Agent(name="Main", instructions="test"),
    )

    assert context.publisher.markers == 0
    assert context.agent_started
    assert not context.review_marked
    assert isinstance(context.activity, ActivityRecorder)
    assert context.activity.activities == []
