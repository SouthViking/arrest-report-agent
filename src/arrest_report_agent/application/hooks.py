"""Deterministic Agents SDK lifecycle side effects."""

from __future__ import annotations

import logging
from typing import Any

from agents import Agent, RunHooks
from agents.run_context import AgentHookContext

from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.domain import AgentActivity, RequestIntent

from .service_context import ReviewRunContext

LOGGER = logging.getLogger(__name__)


class ReviewLifecycleHooks(RunHooks[ReviewRunContext]):
    """Observe the main agent lifecycle without exposing operational tools to the model."""

    async def on_agent_start(
        self,
        context: AgentHookContext[ReviewRunContext],
        agent: Agent[ReviewRunContext],
    ) -> None:
        del agent
        run_context = context.context
        first_start = not run_context.agent_started
        run_context.agent_started = True
        if (
            first_start
            and run_context.activity is not None
            and run_context.request_intent is RequestIntent.REVIEW
            and run_context.has_revisable_material
        ):
            await run_context.activity.transition(AgentActivity.REVIEW)
        if (
            run_context.request_intent is not RequestIntent.REVIEW
            or not run_context.has_revisable_material
            or run_context.review_marked
        ):
            return
        try:
            await run_context.publisher.mark_review_started()
            run_context.review_marked = True
        except Exception as error:
            case = run_context.case
            log_event(
                LOGGER,
                "warning",
                "review_start_marker_failed",
                exc_info=error,
                guild_id=case.guild_id,
                channel_id=case.channel_id,
                root_message_id=case.root_message_id,
                trigger_message_id=case.trigger_message_id,
                attempt=run_context.attempt,
                **exception_fields(error),
            )

    async def on_agent_end(
        self,
        context: AgentHookContext[ReviewRunContext],
        agent: Agent[ReviewRunContext],
        output: Any,
    ) -> None:
        del agent, output
        context.context.agent_completed = True
