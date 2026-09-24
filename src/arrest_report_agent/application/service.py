"""OpenAI Agents SDK review runner."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from agents import (
    Agent,
    ModelSettings,
    RunConfig,
    Runner,
    ToolExecutionConfig,
)
from agents.memory import Session
from agents.retry import ModelRetrySettings
from agents.usage import Usage
from openai.types.shared.reasoning import Reasoning

from arrest_report_agent.config import AppSettings, BotPersona, ResolvedModel
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.domain import AgentAccessLevel, AgentActivity, ReviewCase
from arrest_report_agent.knowledge import (
    ChargeCatalog,
    ChargeCatalogProvider,
    ManualCatalog,
    RadioCodeCatalog,
    ReportGuide,
    as_charge_catalog_provider,
)

from .errors import AgentSessionUnavailableError, classify_error, public_error_message
from .ports import (
    ArrestReportLookup,
    CharacterLegalFactsLookup,
    EvidenceBagLookup,
    FactionFrequencyLookup,
    FactionOfficerRankLookup,
    ReportImageLookup,
)
from .prompt import PROMPT_VERSION, build_agent_instructions
from .service_context import ReviewRunContext
from .session_input import merge_sanitized_session_input
from .tools import build_agent_tools
from .usage import summarize_usage

LOGGER = logging.getLogger(__name__)


class ReviewService:
    """Execute one bounded agent run and publish failures safely."""

    def __init__(
        self,
        settings: AppSettings,
        catalog: ChargeCatalog | ChargeCatalogProvider,
        guide: ReportGuide,
        model: ResolvedModel,
        persona: BotPersona,
        radio_catalog: RadioCodeCatalog | None = None,
        arrest_report_lookup: ArrestReportLookup | None = None,
        character_legal_facts_lookup: CharacterLegalFactsLookup | None = None,
        evidence_bag_lookup: EvidenceBagLookup | None = None,
        faction_frequency_lookup: FactionFrequencyLookup | None = None,
        manual_catalog: ManualCatalog | None = None,
        report_image_lookup: ReportImageLookup | None = None,
        faction_officer_rank_lookup: FactionOfficerRankLookup | None = None,
        max_rank_names_per_lookup: int = 20,
    ) -> None:
        self.settings = settings
        self.catalog_provider = as_charge_catalog_provider(catalog)
        self.guide = guide
        self.model = model
        self.persona = persona
        self.radio_catalog = radio_catalog or RadioCodeCatalog.load_default()
        self.manual_catalog = manual_catalog or ManualCatalog.load_default()
        self.arrest_report_lookup = arrest_report_lookup
        self.report_image_lookup = report_image_lookup
        tools = build_agent_tools(
            self.catalog_provider,
            guide,
            self.radio_catalog,
            max_images_per_review=settings.max_images_per_review,
            max_image_bytes=settings.max_image_bytes,
            max_historical_images=settings.thread_context_max_historical_images,
            max_files_per_review=settings.max_files_per_review,
            max_file_bytes=settings.max_file_bytes,
            max_total_file_bytes=settings.max_total_file_bytes,
            image_detail=model.runtime_defaults.image_detail,
            access_level=AgentAccessLevel.FULL,
            arrest_report_lookup=arrest_report_lookup,
            character_legal_facts_lookup=character_legal_facts_lookup,
            evidence_bag_lookup=evidence_bag_lookup,
            faction_frequency_lookup=faction_frequency_lookup,
            faction_officer_rank_lookup=faction_officer_rank_lookup,
            max_rank_names_per_lookup=max_rank_names_per_lookup,
            report_channel_ids=settings.report_channel_allowlist.ids or frozenset(),
            manual_catalog=self.manual_catalog,
            report_image_lookup=report_image_lookup,
        )
        owner_tools = build_agent_tools(
            self.catalog_provider,
            guide,
            self.radio_catalog,
            max_images_per_review=settings.max_images_per_review,
            max_image_bytes=settings.max_image_bytes,
            max_historical_images=settings.thread_context_max_historical_images,
            max_files_per_review=settings.max_files_per_review,
            max_file_bytes=settings.max_file_bytes,
            max_total_file_bytes=settings.max_total_file_bytes,
            image_detail=model.runtime_defaults.image_detail,
            access_level=AgentAccessLevel.REPORT_OWNER,
            arrest_report_lookup=arrest_report_lookup,
            character_legal_facts_lookup=character_legal_facts_lookup,
            evidence_bag_lookup=evidence_bag_lookup,
            faction_frequency_lookup=None,
            faction_officer_rank_lookup=faction_officer_rank_lookup,
            max_rank_names_per_lookup=max_rank_names_per_lookup,
            report_channel_ids=settings.report_channel_allowlist.ids or frozenset(),
            manual_catalog=self.manual_catalog,
            report_image_lookup=report_image_lookup,
        )
        model_settings = ModelSettings(
            reasoning=Reasoning(effort=model.reasoning_effort),
            verbosity=model.runtime_defaults.text_verbosity,
            max_tokens=model.runtime_defaults.max_output_tokens,
            tool_choice="auto",
            parallel_tool_calls=True,
            retry=ModelRetrySettings(max_retries=settings.openai_max_retries),
            include_usage=True,
            extra_args={"service_tier": model.runtime_defaults.service_tier},
        )
        self.agent: Agent[ReviewRunContext] = Agent(
            name=persona.identity.display_name,
            instructions=build_agent_instructions(
                persona,
                arrest_lookup_enabled=arrest_report_lookup is not None,
                character_legal_facts_enabled=character_legal_facts_lookup is not None,
                evidence_lookup_enabled=evidence_bag_lookup is not None,
                faction_frequency_lookup_enabled=faction_frequency_lookup is not None,
                faction_officer_rank_lookup_enabled=faction_officer_rank_lookup is not None,
                max_rank_names_per_lookup=max_rank_names_per_lookup,
                report_image_lookup_enabled=report_image_lookup is not None,
            ),
            model=model.id,
            model_settings=model_settings,
            tools=tools,  # type: ignore[arg-type]
        )

        self.report_owner_agent: Agent[ReviewRunContext] = Agent(
            name=persona.identity.display_name,
            instructions=build_agent_instructions(
                persona,
                AgentAccessLevel.REPORT_OWNER,
                arrest_lookup_enabled=arrest_report_lookup is not None,
                character_legal_facts_enabled=character_legal_facts_lookup is not None,
                evidence_lookup_enabled=evidence_bag_lookup is not None,
                faction_frequency_lookup_enabled=False,
                faction_officer_rank_lookup_enabled=faction_officer_rank_lookup is not None,
                max_rank_names_per_lookup=max_rank_names_per_lookup,
                report_image_lookup_enabled=report_image_lookup is not None,
            ),
            model=model.id,
            model_settings=model_settings,
            tools=owner_tools,  # type: ignore[arg-type]
        )

    @property
    def catalog(self) -> ChargeCatalog:
        """Return the active catalog for deterministic command invocations."""

        return self.catalog_provider.snapshot().catalog

    async def review(
        self,
        run_context: ReviewRunContext,
        agent_input: list[dict[str, Any]],
        *,
        session: Session | None = None,
        stateless_agent_input: list[dict[str, Any]] | None = None,
    ) -> None:
        if run_context.charge_catalog_snapshot is None:
            run_context.charge_catalog_snapshot = self.catalog_provider.snapshot()
        started_at = time.monotonic()
        case = run_context.case
        run_config = RunConfig(
            tracing_disabled=not self.settings.openai_tracing_enabled,
            trace_include_sensitive_data=self.settings.openai_trace_include_sensitive_data,
            workflow_name=self.settings.trace_workflow_name,
            group_id=f"discord-report:{case.guild_id}:{case.root_message_id}",
            trace_metadata=self._trace_metadata(case, run_context),
            tool_execution=ToolExecutionConfig(max_function_tool_concurrency=6),
            session_input_callback=merge_sanitized_session_input,
        )
        log_event(
            LOGGER,
            "info",
            "review_started",
            model=self.model.id,
            reasoning_effort=self.model.reasoning_effort,
            service_tier=self.model.runtime_defaults.service_tier,
            session_enabled=session is not None,
            **self._log_metadata(case, run_context),
        )
        result = None
        try:
            try:
                result = await self._run_agent(
                    run_context,
                    agent_input,
                    run_config,
                    session=session,
                    started_at=started_at,
                )
            except AgentSessionUnavailableError as session_error:
                if session is None or run_context.published or run_context.messages_sent_count > 0:
                    raise
                log_event(
                    LOGGER,
                    "warning",
                    "agent_session_runtime_fallback",
                    exc_info=session_error,
                    fallback="stateless_full_context",
                    **exception_fields(session_error),
                    **self._log_metadata(case, run_context),
                )
                result = await self._run_agent(
                    run_context,
                    stateless_agent_input or agent_input,
                    run_config,
                    session=None,
                    started_at=started_at,
                )
            if not run_context.published and run_context.messages_sent_count == 0:
                final_output = result.final_output
                if not isinstance(final_output, str) or not final_output.strip():
                    raise RuntimeError("Agent finished without a visible response")
                if run_context.activity is not None:
                    await run_context.activity.transition(AgentActivity.RESPONSE)
                await run_context.publisher.send_message(final_output.strip())
                run_context.messages_sent_count += 1
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
                review_scope=(
                    run_context.publication.review_scope.value
                    if run_context.publication is not None
                    else "not_applicable"
                ),
                latency_ms=round((time.monotonic() - started_at) * 1000),
                model=self.model.id,
                reasoning_effort=self.model.reasoning_effort,
                **usage.to_log_fields(),
                **self._log_metadata(case, run_context),
            )
        except Exception as error:
            classified = classify_error(error)
            usage_source = result.context_wrapper.usage if result is not None else Usage()
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
                **self._log_metadata(case, run_context),
            )
            if run_context.published:
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
                    **self._log_metadata(case, run_context),
                )

    async def _run_agent(
        self,
        run_context: ReviewRunContext,
        agent_input: list[dict[str, Any]],
        run_config: RunConfig,
        *,
        session: Session | None,
        started_at: float,
    ) -> Any:
        remaining_timeout = self.settings.review_timeout_seconds - (time.monotonic() - started_at)
        if remaining_timeout <= 0:
            raise TimeoutError("Review timeout exhausted before the agent run")
        return await asyncio.wait_for(
            Runner.run(
                (
                    self.report_owner_agent
                    if run_context.access_level is AgentAccessLevel.REPORT_OWNER
                    else self.agent
                ),
                agent_input,  # type: ignore[arg-type]
                context=run_context,
                max_turns=self.settings.max_agent_turns,
                run_config=run_config,
                session=session,
            ),
            timeout=remaining_timeout,
        )

    def _log_metadata(
        self,
        case: ReviewCase,
        run_context: ReviewRunContext,
    ) -> dict[str, Any]:
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
            "attempt": run_context.attempt,
            "access_level": run_context.access_level.value,
            "owner_round": run_context.owner_round or "none",
            "owner_max_rounds": run_context.owner_max_rounds or "none",
            "owner_reservation_result": run_context.owner_reservation_result or "none",
            "persona_id": self.persona.id,
            "persona_revision": self.persona.revision,
            "charge_catalog_source": (
                run_context.charge_catalog_snapshot.source.value
                if run_context.charge_catalog_snapshot is not None
                else "unknown"
            ),
            "charge_catalog_hash": (
                run_context.charge_catalog_snapshot.catalog.source_hash
                if run_context.charge_catalog_snapshot is not None
                else "unknown"
            ),
        }

    def _trace_metadata(
        self,
        case: ReviewCase,
        attempt: int | ReviewRunContext,
    ) -> dict[str, Any]:
        if isinstance(attempt, ReviewRunContext):
            run_context = attempt
            attempt_number = run_context.attempt
            access_level = run_context.access_level.value
            owner_round = str(run_context.owner_round or "")
            owner_max_rounds = str(run_context.owner_max_rounds or "")
            owner_reservation_result = run_context.owner_reservation_result or ""
        else:
            attempt_number = attempt
            access_level = AgentAccessLevel.FULL.value
            owner_round = ""
            owner_max_rounds = ""
            owner_reservation_result = ""
        return {
            "environment": self.settings.app_environment,
            "app_version": self.settings.app_version,
            "prompt_version": PROMPT_VERSION,
            "guild_id": str(case.guild_id),
            "parent_channel_id": str(case.parent_channel_id),
            "thread_id": str(case.thread_id or ""),
            "root_message_id": str(case.root_message_id),
            "trigger_message_id": str(case.trigger_message_id),
            "user_id": str(case.requester_user_id),
            "review_attempt": str(attempt_number),
            "access_level": access_level,
            "owner_round": owner_round,
            "owner_max_rounds": owner_max_rounds,
            "owner_reservation_result": owner_reservation_result,
            "model": self.model.id,
            "persona_id": self.persona.id,
        }
