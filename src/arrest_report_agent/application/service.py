"""OpenAI Agents SDK review runner."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from agents import (
    Agent,
    InputGuardrailTripwireTriggered,
    ModelSettings,
    RunConfig,
    Runner,
    StopAtTools,
)
from agents.retry import ModelRetrySettings
from openai.types.shared.reasoning import Reasoning

from arrest_report_agent.config import AppSettings, BotPersona, ResolvedModel
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.domain import ReviewCase
from arrest_report_agent.knowledge import ChargeCatalog, ReportGuide

from .errors import classify_error, public_error_message
from .hooks import ReviewLifecycleHooks
from .prompt import PROMPT_VERSION, build_agent_instructions
from .scope import SCOPE_GUARDRAIL_VERSION, build_scope_guardrail, scope_refusal_message
from .service_context import ReviewRunContext
from .tools import build_agent_tools
from .usage import summarize_usage

LOGGER = logging.getLogger(__name__)


class ReviewService:
    """Execute one bounded agent run and publish failures safely."""

    def __init__(
        self,
        settings: AppSettings,
        catalog: ChargeCatalog,
        guide: ReportGuide,
        model: ResolvedModel,
        persona: BotPersona,
    ) -> None:
        self.settings = settings
        self.catalog = catalog
        self.guide = guide
        self.model = model
        self.persona = persona
        tools = build_agent_tools(catalog, guide)
        scope_guardrail = build_scope_guardrail(settings, model)
        self.hooks = ReviewLifecycleHooks()
        self.agent: Agent[ReviewRunContext] = Agent(
            name=persona.identity.display_name,
            instructions=build_agent_instructions(persona),
            model=model.id,
            model_settings=ModelSettings(
                reasoning=Reasoning(effort=model.reasoning_effort),
                verbosity=model.runtime_defaults.text_verbosity,
                max_tokens=model.runtime_defaults.max_output_tokens,
                tool_choice="required",
                parallel_tool_calls=False,
                retry=ModelRetrySettings(max_retries=settings.openai_max_retries),
                include_usage=True,
                extra_args={"service_tier": model.runtime_defaults.service_tier},
            ),
            tools=tools,  # type: ignore[arg-type]
            input_guardrails=[scope_guardrail],
            tool_use_behavior=StopAtTools(stop_at_tool_names=["finish_interaction", "publish_review"]),
            reset_tool_choice=False,
        )

    async def review(self, run_context: ReviewRunContext, agent_input: list[dict[str, Any]]) -> None:
        started_at = time.monotonic()
        case = run_context.case
        run_config = RunConfig(
            tracing_disabled=not self.settings.openai_tracing_enabled,
            trace_include_sensitive_data=self.settings.openai_trace_include_sensitive_data,
            workflow_name=self.settings.trace_workflow_name,
            group_id=f"discord-report:{case.guild_id}:{case.root_message_id}",
            trace_metadata=self._trace_metadata(case, run_context.attempt),
        )
        log_event(
            LOGGER,
            "info",
            "review_started",
            model=self.model.id,
            reasoning_effort=self.model.reasoning_effort,
            service_tier=self.model.runtime_defaults.service_tier,
            **self._log_metadata(case, run_context.attempt),
        )
        result = None
        try:
            result = await asyncio.wait_for(
                Runner.run(
                    self.agent,
                    agent_input,  # type: ignore[arg-type]
                    context=run_context,
                    max_turns=self.settings.max_agent_turns,
                    hooks=self.hooks,
                    run_config=run_config,
                ),
                timeout=self.settings.review_timeout_seconds,
            )
            if not run_context.published and not run_context.interaction_completed:
                raise RuntimeError("Agent finished without a terminal response tool")
            if run_context.published and run_context.publication is None:
                raise RuntimeError("Agent published a review without a publication payload")
            usage = summarize_usage(result.context_wrapper.usage, self.model)
            outcome = "review" if run_context.published else "conversation"
            log_event(
                LOGGER,
                "info",
                "review_completed",
                outcome=outcome,
                verdict=(
                    run_context.publication.verdict.value
                    if run_context.publication is not None
                    else "not_applicable"
                ),
                latency_ms=round((time.monotonic() - started_at) * 1000),
                model=self.model.id,
                reasoning_effort=self.model.reasoning_effort,
                **usage.to_log_fields(),
                **self._log_metadata(case, run_context.attempt),
            )
        except InputGuardrailTripwireTriggered as error:
            usage = summarize_usage(run_context.guardrail_usage, self.model)
            log_event(
                LOGGER,
                "info",
                "scope_guardrail_blocked",
                latency_ms=round((time.monotonic() - started_at) * 1000),
                guardrail_name=error.guardrail_result.guardrail.get_name(),
                **usage.to_log_fields(),
                **self._log_metadata(case, run_context.attempt),
            )
            try:
                await run_context.publisher.send_message(
                    scope_refusal_message(
                        self.persona.messages.access,
                        self.persona.identity.default_form_of_address,
                    )
                )
                run_context.message_sent = True
                run_context.messages_sent_count += 1
                run_context.interaction_completed = True
            except Exception as publication_error:
                log_event(
                    LOGGER,
                    "error",
                    "scope_refusal_publication_failed",
                    exc_info=publication_error,
                    **exception_fields(publication_error),
                    **self._log_metadata(case, run_context.attempt),
                )
        except Exception as error:
            classified = classify_error(error)
            usage_source = result.context_wrapper.usage if result is not None else run_context.guardrail_usage
            usage = summarize_usage(usage_source, self.model)
            technical_reference = f"{case.root_message_id}-{run_context.attempt}-{classified.kind.value}"
            log_event(
                LOGGER,
                "error",
                "review_failed",
                exc_info=error,
                error_kind=classified.kind.value,
                retryable=classified.retryable,
                latency_ms=round((time.monotonic() - started_at) * 1000),
                technical_reference=technical_reference,
                **usage.to_log_fields(),
                **exception_fields(error),
                **self._log_metadata(case, run_context.attempt),
            )
            if run_context.published or run_context.interaction_completed:
                return
            try:
                await run_context.publisher.publish_failure(
                    public_error_message(classified.kind, self.persona.messages.failures),
                )
            except Exception as publication_error:
                log_event(
                    LOGGER,
                    "error",
                    "failure_publication_failed",
                    exc_info=publication_error,
                    technical_reference=technical_reference,
                    **exception_fields(publication_error),
                    **self._log_metadata(case, run_context.attempt),
                )

    def _log_metadata(self, case: ReviewCase, attempt: int) -> dict[str, Any]:
        """Return filterable Discord identifiers shared by every runner event."""

        return {
            "guild_id": case.guild_id,
            "parent_channel_id": case.parent_channel_id,
            "channel_id": case.channel_id,
            "thread_id": case.thread_id if case.thread_id is not None else "none",
            "user_id": case.requester_user_id,
            "message_id": case.trigger_message_id,
            "root_message_id": case.root_message_id,
            "trigger_message_id": case.trigger_message_id,
            "attempt": attempt,
            "persona_id": self.persona.id,
            "persona_revision": self.persona.revision,
        }

    def _trace_metadata(self, case: ReviewCase, attempt: int) -> dict[str, Any]:
        return {
            "environment": self.settings.app_environment,
            "app_version": self.settings.app_version,
            "prompt_version": PROMPT_VERSION,
            "scope_guardrail_version": SCOPE_GUARDRAIL_VERSION,
            "guild_id": str(case.guild_id),
            "parent_channel_id": str(case.parent_channel_id),
            "thread_id": str(case.thread_id or ""),
            "root_message_id": str(case.root_message_id),
            "trigger_message_id": str(case.trigger_message_id),
            "user_id": str(case.requester_user_id),
            "review_attempt": str(attempt),
            "model": self.model.id,
            "reasoning_effort": self.model.reasoning_effort,
            "persona_id": self.persona.id,
        }
