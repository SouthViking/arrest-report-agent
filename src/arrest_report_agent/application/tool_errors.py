"""Bounded recovery for SDK argument errors, never for failed side effects."""

from __future__ import annotations

import json
import logging

from agents import RunContextWrapper
from agents.exceptions import ModelBehaviorError
from agents.tool_context import ToolContext

from arrest_report_agent.config.logging import log_event

from .service_context import ReviewRunContext

LOGGER = logging.getLogger(__name__)
MAX_TOOL_ARGUMENT_RECOVERIES = 2


def recover_tool_argument_error(ctx: RunContextWrapper[ReviewRunContext], error: Exception) -> str:
    """Let the model repair pre-execution arguments within the current run.

    Tools using this callback must mark their call ID when their body starts.
    This also keeps an internal ModelBehaviorError from being mistaken for an
    SDK parsing/validation error, even if it has the same error message.
    """

    if (
        not isinstance(ctx, ToolContext)
        or not isinstance(error, ModelBehaviorError)
        or not str(error).startswith(f"Invalid JSON input for tool {ctx.tool_name}")
        or ctx.tool_call_id in ctx.context.started_tool_call_ids
    ):
        raise error

    run_context = ctx.context
    recovery_count = run_context.tool_argument_recovery_counts.get(ctx.tool_name, 0) + 1
    run_context.tool_argument_recovery_counts[ctx.tool_name] = recovery_count
    exhausted = recovery_count > MAX_TOOL_ARGUMENT_RECOVERIES
    case = run_context.case
    # Do not log raw arguments or the exception text: either may contain report data.
    log_event(
        LOGGER,
        "error" if exhausted else "warning",
        "tool_arguments_recovery_exhausted" if exhausted else "tool_arguments_recovery_requested",
        tool_name=ctx.tool_name,
        tool_call_id=ctx.tool_call_id,
        error_type=type(error).__name__,
        recovery_count=recovery_count,
        max_recoveries=MAX_TOOL_ARGUMENT_RECOVERIES,
        guild_id=case.guild_id,
        channel_id=case.channel_id,
        thread_id=case.thread_id,
        root_message_id=case.root_message_id,
        trigger_message_id=case.trigger_message_id,
        user_id=case.requester_user_id,
        attempt=run_context.attempt,
    )
    if exhausted:
        run_context.tool_argument_recovery_exhausted = True
        raise error

    return json.dumps(
        {
            "ok": False,
            "error": "invalid_tool_arguments",
            "tool_name": ctx.tool_name,
            "retryable": True,
            "recovery_attempt": recovery_count,
            "remaining_recoveries": MAX_TOOL_ARGUMENT_RECOVERIES - recovery_count,
            "instruction": (
                "This tool was not executed. Correct the JSON arguments to match the tool schema "
                "and its field constraints, then call this tool again. "
                "Do not repeat other actions already confirmed by tools."
            ),
        },
        separators=(",", ":"),
    )
