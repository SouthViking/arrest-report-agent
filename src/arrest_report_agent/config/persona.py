"""Validated, provider-backed bot personas loaded once at startup."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import PurePosixPath
from string import Formatter
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PERSONA_SCHEMA_VERSION = 27
PERSONA_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
MAX_PROFILE_ASSET_BYTES = 10_485_760
SUPPORTED_PROFILE_ASSET_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif"})


class PersonaConfigurationError(RuntimeError):
    """Raised when a selected persona or its avatar is invalid."""


class FrozenPersonaModel(BaseModel):
    """Common strict configuration for immutable persona sections."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class DiscordPersona(FrozenPersonaModel):
    """Global Discord profile fields managed by the bot."""

    username: str = Field(min_length=2, max_length=32)
    nickname: str | None = Field(default=None, min_length=1, max_length=32)
    bio: str = Field(min_length=1, max_length=190)
    avatar: str = Field(min_length=1)


class PersonaIdentity(FrozenPersonaModel):
    """In-character identity used by prompts and deterministic Discord copy."""

    display_name: str = Field(min_length=1, max_length=100)
    full_name: str = Field(min_length=1, max_length=100)
    rank: str = Field(min_length=1, max_length=100)
    role_title: str = Field(min_length=1, max_length=100)
    organization_name: str = Field(min_length=1, max_length=200)
    organization_acronym: str = Field(min_length=1, max_length=30)
    default_form_of_address: str = Field(min_length=1, max_length=50)
    language: str = Field(min_length=2, max_length=16)
    setting: str = Field(min_length=1, max_length=300)


class PersonaCommunication(FrozenPersonaModel):
    """Persona voice split between natural conversation and review summaries."""

    conversation: tuple[str, ...] = Field(min_length=1, max_length=12)
    review_summary: tuple[str, ...] = Field(min_length=1, max_length=8)

    @field_validator("conversation", "review_summary")
    @classmethod
    def reject_empty_items(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("persona communication lists cannot contain empty items")
        return values


class PersonaConversationExample(FrozenPersonaModel):
    """One compact natural-conversation example for the selected voice."""

    situation: str = Field(min_length=1, max_length=100)
    user: str = Field(min_length=1, max_length=300)
    assistant: str = Field(min_length=1, max_length=500)


class PersonaReviewSummaryExample(FrozenPersonaModel):
    """One example limited to the personalized opening of a review result."""

    verdict: Literal["approved", "corrections_required", "inconclusive"]
    situation: str = Field(min_length=1, max_length=150)
    summary: str = Field(min_length=1, max_length=500)


class PersonaExamples(FrozenPersonaModel):
    """Bounded few-shot examples kept separate by interaction context."""

    conversation: tuple[PersonaConversationExample, ...] = Field(
        min_length=1,
        max_length=6,
    )
    review_summary: tuple[PersonaReviewSummaryExample, ...] = Field(
        min_length=1,
        max_length=4,
    )


class PersonaVoice(FrozenPersonaModel):
    """Compact traits and context-specific voices rendered into the agent prompt."""

    summary: str = Field(min_length=1, max_length=500)
    traits: tuple[str, ...] = Field(min_length=1, max_length=12)
    communication: PersonaCommunication
    examples: PersonaExamples
    boundaries: tuple[str, ...] = Field(min_length=1, max_length=12)

    @field_validator("traits", "boundaries")
    @classmethod
    def reject_empty_items(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("persona lists cannot contain empty items")
        return values


class PersonaPresence(FrozenPersonaModel):
    """Discord presence copy for each supported activity mode."""

    idle: str = Field(min_length=1, max_length=128)
    conversation: str = Field(min_length=1, max_length=128)
    knowledge: str = Field(min_length=1, max_length=128)
    review: str = Field(min_length=1, max_length=128)


class PersonaAccessMessages(FrozenPersonaModel):
    """Deterministic access and command notices written in the persona's voice."""

    unauthorized_user_intros: tuple[str, ...] = Field(min_length=1, max_length=8)
    unauthorized_user: str = Field(min_length=1, max_length=400)
    blocked_user_messages: tuple[str, ...] = Field(min_length=2, max_length=8)
    scope_refusal: str = Field(min_length=1, max_length=400)
    supervisor_unauthorized: str = Field(min_length=1, max_length=400)
    other_member_query: str = Field(min_length=1, max_length=250)
    other_supervisor_query: str = Field(min_length=1, max_length=250)
    personal_wrong_guild: str = Field(min_length=1, max_length=300)
    personal_member_unavailable: str = Field(min_length=1, max_length=300)
    supervisor_wrong_guild: str = Field(min_length=1, max_length=300)
    supervisor_member_unavailable: str = Field(min_length=1, max_length=300)
    no_supervisor_channels: str = Field(min_length=1, max_length=300)
    no_personal_channels: str = Field(min_length=1, max_length=300)

    @field_validator("unauthorized_user_intros", "blocked_user_messages")
    @classmethod
    def reject_empty_pool_items(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("persona message pools cannot contain empty items")
        return values

    @model_validator(mode="after")
    def validate_templates(self) -> PersonaAccessMessages:
        _require_template_fields(
            self.unauthorized_user,
            {"form_of_address", "intro"},
            "unauthorized_user",
        )
        _require_template_fields(
            self.scope_refusal,
            {"form_of_address"},
            "scope_refusal",
        )
        return self


class PersonaFailureMessages(FrozenPersonaModel):
    """Safe deterministic operational failures without provider details."""

    quota_exceeded: str = Field(min_length=1, max_length=500)
    rate_limited: str = Field(min_length=1, max_length=500)
    timeout: str = Field(min_length=1, max_length=500)
    provider_internal: str = Field(min_length=1, max_length=500)
    auth_configuration: str = Field(min_length=1, max_length=500)
    agent_execution: str = Field(min_length=1, max_length=500)
    discord_failure: str = Field(min_length=1, max_length=500)
    context_unavailable: str = Field(min_length=1, max_length=500)
    unknown: str = Field(min_length=1, max_length=500)
    review_failure_title: str = Field(min_length=1, max_length=256)
    review_failure_next_step: str = Field(min_length=1, max_length=500)
    charge_query_failure: str = Field(min_length=1, max_length=500)
    personal_report_query_failure: str = Field(min_length=1, max_length=500)
    personal_summary_failure: str = Field(min_length=1, max_length=500)
    supervision_query_failure: str = Field(min_length=1, max_length=500)
    shame_query_failure: str = Field(min_length=1, max_length=500)
    honor_query_failure: str = Field(min_length=1, max_length=500)


class PersonaHelpMessages(FrozenPersonaModel):
    """Personality-bearing copy surrounding the functional help fields."""

    description: str = Field(min_length=1, max_length=700)
    title: str = Field(min_length=1, max_length=256)
    review_scope_title: str = Field(min_length=1, max_length=256)
    review_scope: str = Field(min_length=1, max_length=900)
    location_title: str = Field(min_length=1, max_length=256)
    unrestricted_channels: str = Field(min_length=1, max_length=300)
    request_review_title: str = Field(min_length=1, max_length=256)
    request_review: str = Field(min_length=1, max_length=900)
    thread_behavior_title: str = Field(min_length=1, max_length=256)
    thread_behavior: str = Field(min_length=1, max_length=900)
    personal_commands_title: str = Field(min_length=1, max_length=256)
    personal_commands: str = Field(min_length=1, max_length=900)
    general_commands_title: str = Field(min_length=1, max_length=256)
    general_commands: str = Field(min_length=1, max_length=1024)
    supervision_commands_title: str = Field(min_length=1, max_length=256)
    supervision_commands: str = Field(min_length=1, max_length=900)
    official_docs_title: str = Field(min_length=1, max_length=256)
    official_docs: str = Field(min_length=1, max_length=900)
    footer: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_templates(self) -> PersonaHelpMessages:
        _require_template_fields(
            self.title,
            {"display_name", "role_title"},
            "help.title",
        )
        _require_template_fields(
            self.description,
            {"display_name", "role_title"},
            "help.description",
        )
        _require_template_fields(
            self.request_review,
            {"bot_mention"},
            "help.request_review",
        )
        _require_template_fields(
            self.official_docs,
            {"penal_code_url", "procedures_manual_url"},
            "help.official_docs",
        )
        _require_template_fields(
            self.footer,
            {"form_of_address"},
            "help.footer",
        )
        return self


class PersonaNavigationMessages(FrozenPersonaModel):
    """Shared labels for deterministic selectors, links and pagination."""

    previous_button: str = Field(min_length=1, max_length=80)
    next_button: str = Field(min_length=1, max_length=80)
    all_stations: str = Field(min_length=1, max_length=100)
    period_current_month: str = Field(min_length=1, max_length=100)
    period_last_7_days: str = Field(min_length=1, max_length=100)
    period_last_30_days: str = Field(min_length=1, max_length=100)
    period_last_90_days: str = Field(min_length=1, max_length=100)
    status_filter_all: str = Field(min_length=1, max_length=100)
    status_filter_pending: str = Field(min_length=1, max_length=100)
    status_filter_approved: str = Field(min_length=1, max_length=100)
    status_filter_corrections_required: str = Field(min_length=1, max_length=100)
    status_filter_inconclusive: str = Field(min_length=1, max_length=100)
    status_pending: str = Field(min_length=1, max_length=100)
    status_in_review: str = Field(min_length=1, max_length=100)
    status_approved: str = Field(min_length=1, max_length=100)
    status_corrections_required: str = Field(min_length=1, max_length=100)
    status_inconclusive: str = Field(min_length=1, max_length=100)
    open_post: str = Field(min_length=1, max_length=100)
    open_thread: str = Field(min_length=1, max_length=100)
    unknown_author: str = Field(min_length=1, max_length=100)


class PersonaKnowledgeMessages(FrozenPersonaModel):
    """Personality-aware UI copy for guide, FAQ and charge lookup commands."""

    guide_selector_placeholder: str = Field(min_length=1, max_length=150)
    guide_title: str = Field(min_length=1, max_length=256)
    guide_description: str = Field(min_length=1, max_length=600)
    guide_footer: str = Field(min_length=1, max_length=300)
    faq_selector_placeholder: str = Field(min_length=1, max_length=150)
    faq_title: str = Field(min_length=1, max_length=256)
    faq_description: str = Field(min_length=1, max_length=700)
    faq_footer: str = Field(min_length=1, max_length=300)
    faq_reference_title: str = Field(min_length=1, max_length=256)
    faq_reference: str = Field(min_length=1, max_length=500)
    charge_query_placeholder: str = Field(min_length=1, max_length=150)
    charge_all_categories: str = Field(min_length=1, max_length=100)
    charge_category_count: str = Field(min_length=1, max_length=100)
    charge_query_label: str = Field(min_length=1, max_length=100)
    charge_query_description: str = Field(min_length=1, max_length=200)
    charge_category_label: str = Field(min_length=1, max_length=100)
    charge_modal_title: str = Field(min_length=1, max_length=100)
    charge_result_title: str = Field(min_length=1, max_length=256)
    charge_result_description: str = Field(min_length=1, max_length=700)
    charge_empty_title: str = Field(min_length=1, max_length=256)
    charge_empty_body: str = Field(min_length=1, max_length=600)
    charge_result_category_label: str = Field(min_length=1, max_length=100)
    charge_result_type_label: str = Field(min_length=1, max_length=100)
    charge_sentence_label: str = Field(min_length=1, max_length=100)
    charge_parole_label: str = Field(min_length=1, max_length=100)
    charge_fine_label: str = Field(min_length=1, max_length=100)
    charge_points_label: str = Field(min_length=1, max_length=100)
    charge_penalty_missing: str = Field(min_length=1, max_length=200)
    charge_footer: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_templates(self) -> PersonaKnowledgeMessages:
        templates = (
            (self.guide_title, {"section"}, "interface.knowledge.guide_title"),
            (
                self.guide_description,
                {"procedures_manual_url"},
                "interface.knowledge.guide_description",
            ),
            (
                self.guide_footer,
                {"version", "display_name"},
                "interface.knowledge.guide_footer",
            ),
            (self.faq_description, {"topics"}, "interface.knowledge.faq_description"),
            (self.faq_footer, {"display_name"}, "interface.knowledge.faq_footer"),
            (
                self.faq_reference,
                {"section", "procedures_manual_url"},
                "interface.knowledge.faq_reference",
            ),
            (
                self.charge_category_count,
                {"count"},
                "interface.knowledge.charge_category_count",
            ),
            (
                self.charge_result_description,
                {"query", "penal_code_url"},
                "interface.knowledge.charge_result_description",
            ),
            (
                self.charge_footer,
                {"page", "page_count", "count", "display_name"},
                "interface.knowledge.charge_footer",
            ),
        )
        for template, fields, label in templates:
            _require_template_fields(template, fields, label)
        return self


class PersonaPersonalReportMessages(FrozenPersonaModel):
    """Personality-aware UI copy for personal report commands."""

    period_label: str = Field(min_length=1, max_length=100)
    period_description: str = Field(min_length=1, max_length=200)
    status_label: str = Field(min_length=1, max_length=100)
    station_label: str = Field(min_length=1, max_length=100)
    modal_title: str = Field(min_length=1, max_length=100)
    reports_title: str = Field(min_length=1, max_length=256)
    reports_description: str = Field(min_length=1, max_length=600)
    empty_title: str = Field(min_length=1, max_length=256)
    empty_body: str = Field(min_length=1, max_length=500)
    reports_footer: str = Field(min_length=1, max_length=300)
    reports_footer_truncated: str = Field(min_length=1, max_length=200)
    summary_title: str = Field(min_length=1, max_length=256)
    summary_description: str = Field(min_length=1, max_length=900)
    summary_navigation_title: str = Field(min_length=1, max_length=256)
    summary_navigation_body: str = Field(min_length=1, max_length=500)
    summary_footer: str = Field(min_length=1, max_length=300)
    summary_footer_truncated: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_templates(self) -> PersonaPersonalReportMessages:
        templates = (
            (
                self.reports_description,
                {"period", "status", "station"},
                "interface.personal_reports.reports_description",
            ),
            (
                self.reports_footer,
                {"page", "page_count", "count", "display_name"},
                "interface.personal_reports.reports_footer",
            ),
            (self.summary_title, {"period"}, "interface.personal_reports.summary_title"),
            (
                self.summary_description,
                {
                    "total",
                    "approved",
                    "corrections_required",
                    "pending",
                    "inconclusive",
                    "approval_rate",
                },
                "interface.personal_reports.summary_description",
            ),
            (
                self.summary_footer,
                {"display_name"},
                "interface.personal_reports.summary_footer",
            ),
        )
        for template, fields, label in templates:
            _require_template_fields(template, fields, label)
        return self


class PersonaSupervisionMessages(FrozenPersonaModel):
    """Personality-aware UI copy for report rankings and supervisor commands."""

    period_label: str = Field(min_length=1, max_length=100)
    station_label: str = Field(min_length=1, max_length=100)
    ranking_order_label: str = Field(min_length=1, max_length=100)
    pending_modal_title: str = Field(min_length=1, max_length=100)
    ranking_modal_title: str = Field(min_length=1, max_length=100)
    shame_modal_title: str = Field(min_length=1, max_length=100)
    metric_submitted: str = Field(min_length=1, max_length=100)
    metric_approved: str = Field(min_length=1, max_length=100)
    metric_corrections_required: str = Field(min_length=1, max_length=100)
    metric_approval_rate: str = Field(min_length=1, max_length=100)
    pending_title: str = Field(min_length=1, max_length=256)
    pending_description: str = Field(min_length=1, max_length=700)
    pending_empty_title: str = Field(min_length=1, max_length=256)
    pending_empty_body: str = Field(min_length=1, max_length=500)
    pending_footer: str = Field(min_length=1, max_length=300)
    pending_footer_truncated: str = Field(min_length=1, max_length=200)
    ranking_title: str = Field(min_length=1, max_length=256)
    ranking_description: str = Field(min_length=1, max_length=900)
    ranking_empty_body: str = Field(min_length=1, max_length=500)
    ranking_footer: str = Field(min_length=1, max_length=300)
    ranking_footer_truncated: str = Field(min_length=1, max_length=200)
    ranking_value_approved: str = Field(min_length=1, max_length=150)
    ranking_value_corrections_required: str = Field(min_length=1, max_length=150)
    ranking_value_approval_rate: str = Field(min_length=1, max_length=150)
    ranking_value_submitted: str = Field(min_length=1, max_length=150)
    shame_title: str = Field(min_length=1, max_length=256)
    shame_description: str = Field(min_length=1, max_length=900)
    shame_legend: str = Field(min_length=1, max_length=500)
    shame_empty_body: str = Field(min_length=1, max_length=500)
    shame_footer: str = Field(min_length=1, max_length=300)
    shame_footer_truncated: str = Field(min_length=1, max_length=200)
    shame_value: str = Field(min_length=1, max_length=150)
    honor_modal_title: str = Field(min_length=1, max_length=100)
    honor_title: str = Field(min_length=1, max_length=256)
    honor_description: str = Field(min_length=1, max_length=700)
    honor_empty_body: str = Field(min_length=1, max_length=500)
    honor_footer: str = Field(min_length=1, max_length=300)
    honor_footer_truncated: str = Field(min_length=1, max_length=200)
    honor_value: str = Field(min_length=1, max_length=200)
    report_errors_modal_title: str = Field(min_length=1, max_length=100)
    report_errors_severity_label: str = Field(min_length=1, max_length=100)
    report_errors_limit_label: str = Field(min_length=1, max_length=100)
    report_errors_severity_all: str = Field(min_length=1, max_length=100)
    report_errors_severity_required: str = Field(min_length=1, max_length=100)
    report_errors_severity_warning: str = Field(min_length=1, max_length=100)
    report_errors_severity_observation: str = Field(min_length=1, max_length=100)
    report_errors_title: str = Field(min_length=1, max_length=256)
    report_errors_description: str = Field(min_length=1, max_length=900)
    report_errors_empty_body: str = Field(min_length=1, max_length=500)
    report_errors_footer: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_templates(self) -> PersonaSupervisionMessages:
        templates = (
            (
                self.pending_description,
                {"period", "station"},
                "interface.supervision.pending_description",
            ),
            (
                self.pending_footer,
                {"page", "page_count", "count", "display_name"},
                "interface.supervision.pending_footer",
            ),
            (
                self.ranking_description,
                {"metric", "period", "station", "entries"},
                "interface.supervision.ranking_description",
            ),
            (
                self.ranking_footer,
                {"page", "page_count", "count", "display_name"},
                "interface.supervision.ranking_footer",
            ),
            (self.ranking_value_approved, {"count"}, "interface.supervision.ranking_value_approved"),
            (
                self.ranking_value_corrections_required,
                {"count"},
                "interface.supervision.ranking_value_corrections_required",
            ),
            (
                self.ranking_value_approval_rate,
                {"rate"},
                "interface.supervision.ranking_value_approval_rate",
            ),
            (
                self.ranking_value_submitted,
                {"count"},
                "interface.supervision.ranking_value_submitted",
            ),
            (
                self.shame_description,
                {"period", "entries"},
                "interface.supervision.shame_description",
            ),
            (
                self.shame_footer,
                {"shown", "count", "display_name"},
                "interface.supervision.shame_footer",
            ),
            (self.shame_value, {"count"}, "interface.supervision.shame_value"),
            (
                self.honor_description,
                {"period"},
                "interface.supervision.honor_description",
            ),
            (
                self.honor_footer,
                {"shown", "count", "display_name"},
                "interface.supervision.honor_footer",
            ),
            (
                self.honor_value,
                {"approved", "total", "rate"},
                "interface.supervision.honor_value",
            ),
            (
                self.report_errors_description,
                {"period", "station", "severity", "reports", "attempts"},
                "interface.supervision.report_errors_description",
            ),
            (
                self.report_errors_footer,
                {"shown", "total", "display_name"},
                "interface.supervision.report_errors_footer",
            ),
        )
        for template, fields, label in templates:
            _require_template_fields(template, fields, label)
        return self


class PersonaRoleRequestMessages(FrozenPersonaModel):
    """Personality-aware copy for the deterministic role-request subsystem."""

    alert_title: str = Field(min_length=1, max_length=256)
    pending_title: str = Field(min_length=1, max_length=256)
    pending_description: str = Field(min_length=1, max_length=500)
    status_pending: str = Field(min_length=1, max_length=100)
    status_approved: str = Field(min_length=1, max_length=100)
    status_partial: str = Field(min_length=1, max_length=100)
    status_rejected: str = Field(min_length=1, max_length=100)
    status_failed: str = Field(min_length=1, max_length=100)
    no_available_channels: str = Field(min_length=1, max_length=200)
    unknown_role_label: str = Field(min_length=1, max_length=100)
    created_confirmation: str = Field(min_length=1, max_length=400)
    approve_all_button: str = Field(min_length=1, max_length=80)
    approve_partial_button: str = Field(min_length=1, max_length=80)
    reject_button: str = Field(min_length=1, max_length=80)
    reviewer_unauthorized: str = Field(min_length=1, max_length=300)
    wrong_guild: str = Field(min_length=1, max_length=300)
    thread_blocked: str = Field(min_length=1, max_length=300)
    wrong_channel: str = Field(min_length=1, max_length=500)
    member_unavailable: str = Field(min_length=1, max_length=300)
    subsystem_unavailable: str = Field(min_length=1, max_length=300)
    no_requestable_roles: str = Field(min_length=1, max_length=300)
    no_changes: str = Field(min_length=1, max_length=300)
    too_many_roles: str = Field(min_length=1, max_length=300)
    unlisted_roles: str = Field(min_length=1, max_length=500)
    overlapping_roles: str = Field(min_length=1, max_length=500)
    already_owned_roles: str = Field(min_length=1, max_length=500)
    missing_roles: str = Field(min_length=1, max_length=500)
    protected_roles: str = Field(min_length=1, max_length=500)
    pending_exists: str = Field(min_length=1, max_length=400)
    partial_empty: str = Field(min_length=1, max_length=300)
    stale_request: str = Field(min_length=1, max_length=300)
    request_unavailable: str = Field(min_length=1, max_length=300)
    operation_failed: str = Field(min_length=1, max_length=300)
    info_title: str = Field(min_length=1, max_length=256)
    info_description: str = Field(min_length=1, max_length=600)
    info_usage_title: str = Field(min_length=1, max_length=256)
    info_usage: str = Field(min_length=1, max_length=800)
    info_rules_title: str = Field(min_length=1, max_length=256)
    info_rules: str = Field(min_length=1, max_length=900)
    info_channels_title: str = Field(min_length=1, max_length=256)
    info_all_channels: str = Field(min_length=1, max_length=300)
    info_outcomes_title: str = Field(min_length=1, max_length=256)
    info_outcomes: str = Field(min_length=1, max_length=700)

    @model_validator(mode="after")
    def validate_templates(self) -> PersonaRoleRequestMessages:
        templates = (
            (
                self.pending_description,
                set[str](),
                "interface.role_requests.pending_description",
            ),
            (
                self.created_confirmation,
                {"jump_url"},
                "interface.role_requests.created_confirmation",
            ),
            (
                self.wrong_channel,
                {"channels"},
                "interface.role_requests.wrong_channel",
            ),
            (
                self.overlapping_roles,
                {"roles"},
                "interface.role_requests.overlapping_roles",
            ),
            (
                self.too_many_roles,
                {"limit"},
                "interface.role_requests.too_many_roles",
            ),
            (
                self.unlisted_roles,
                {"roles"},
                "interface.role_requests.unlisted_roles",
            ),
            (
                self.already_owned_roles,
                {"roles"},
                "interface.role_requests.already_owned_roles",
            ),
            (
                self.missing_roles,
                {"roles"},
                "interface.role_requests.missing_roles",
            ),
            (
                self.protected_roles,
                {"roles"},
                "interface.role_requests.protected_roles",
            ),
            (
                self.pending_exists,
                {"jump_url"},
                "interface.role_requests.pending_exists",
            ),
            (
                self.unknown_role_label,
                {"role_id"},
                "interface.role_requests.unknown_role_label",
            ),
        )
        for template, fields, label in templates:
            _require_template_fields(template, fields, label)
        return self


class PersonaPagerMessages(FrozenPersonaModel):
    """Personality-aware framing for the deterministic pager subsystem."""

    alert_title: str = Field(min_length=1, max_length=256)
    unavailable: str = Field(min_length=1, max_length=400)
    wrong_channel: str = Field(min_length=1, max_length=500)
    unauthorized_creator: str = Field(min_length=1, max_length=400)
    unauthorized_closer: str = Field(min_length=1, max_length=400)
    unauthorized_editor: str = Field(min_length=1, max_length=400)
    creator_cannot_respond: str = Field(min_length=1, max_length=400)
    invalid_recipient: str = Field(min_length=1, max_length=400)
    stale: str = Field(min_length=1, max_length=300)
    operation_failed: str = Field(min_length=1, max_length=300)
    info_intro: str = Field(min_length=1, max_length=600)
    info_closing: str = Field(min_length=1, max_length=400)

    @model_validator(mode="after")
    def validate_templates(self) -> PersonaPagerMessages:
        for template, fields, label in (
            (self.wrong_channel, {"channels"}, "interface.pagers.wrong_channel"),
        ):
            _require_template_fields(template, fields, label)
        return self


class PersonaCallsignMessages(FrozenPersonaModel):
    """Personality-aware responses for operational member callsigns."""

    alert_title: str = Field(min_length=1, max_length=256)
    unauthorized: str = Field(min_length=1, max_length=400)
    saved_title: str = Field(min_length=1, max_length=256)
    saved_description: str = Field(min_length=1, max_length=400)
    removed_title: str = Field(min_length=1, max_length=256)
    removed_description: str = Field(min_length=1, max_length=400)
    missing_description: str = Field(min_length=1, max_length=400)
    invalid_format: str = Field(min_length=1, max_length=500)
    operation_failed: str = Field(min_length=1, max_length=300)
    directory_intro: str = Field(min_length=1, max_length=500)
    directory_empty: str = Field(min_length=1, max_length=300)
    navigation_owner_only: str = Field(min_length=1, max_length=300)
    info_intro: str = Field(min_length=1, max_length=500)
    info_closing: str = Field(min_length=1, max_length=400)

    @model_validator(mode="after")
    def validate_templates(self) -> PersonaCallsignMessages:
        _require_template_fields(
            self.saved_description,
            {"callsign"},
            "interface.callsigns.saved_description",
        )
        return self


class PersonaDeploymentMessages(FrozenPersonaModel):
    """Personality-aware framing for deterministic deployment notifications."""

    release_intro: str = Field(min_length=1, max_length=500)
    release_closing: str = Field(min_length=1, max_length=300)
    failed_description: str = Field(min_length=1, max_length=500)
    crashed_description: str = Field(min_length=1, max_length=500)
    oom_description: str = Field(min_length=1, max_length=500)


class PersonaSuggestionMessages(FrozenPersonaModel):
    """Personality-aware copy for the private deterministic suggestion inbox."""

    alert_title: str = Field(min_length=1, max_length=256)
    submitted_title: str = Field(min_length=1, max_length=256)
    submitted_description: str = Field(min_length=1, max_length=500)
    limit_reached: str = Field(min_length=1, max_length=500)
    cooldown: str = Field(min_length=1, max_length=400)
    operation_failed: str = Field(min_length=1, max_length=400)
    owner_unauthorized: str = Field(min_length=1, max_length=400)
    inbox_intro: str = Field(min_length=1, max_length=500)
    inbox_empty: str = Field(min_length=1, max_length=300)
    inbox_owner_only: str = Field(min_length=1, max_length=300)
    status_pending: str = Field(min_length=1, max_length=100)
    status_archived: str = Field(min_length=1, max_length=100)
    status_updated_title: str = Field(min_length=1, max_length=256)
    status_updated_description: str = Field(min_length=1, max_length=400)
    status_unchanged_description: str = Field(min_length=1, max_length=400)
    invalid_reference: str = Field(min_length=1, max_length=300)
    not_found: str = Field(min_length=1, max_length=300)
    info_intro: str = Field(min_length=1, max_length=600)
    info_usage: str = Field(min_length=1, max_length=600)
    info_limit: str = Field(min_length=1, max_length=400)
    info_warning: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_templates(self) -> PersonaSuggestionMessages:
        for template, fields, label in (
            (
                self.submitted_description,
                {"suggestion_id"},
                "interface.suggestions.submitted_description",
            ),
            (self.limit_reached, {"limit"}, "interface.suggestions.limit_reached"),
            (self.cooldown, {"seconds"}, "interface.suggestions.cooldown"),
            (
                self.status_updated_description,
                {"suggestion_id", "status"},
                "interface.suggestions.status_updated_description",
            ),
            (
                self.status_unchanged_description,
                {"suggestion_id", "status"},
                "interface.suggestions.status_unchanged_description",
            ),
            (self.not_found, {"suggestion_id"}, "interface.suggestions.not_found"),
            (self.info_limit, {"limit"}, "interface.suggestions.info_limit"),
        ):
            _require_template_fields(template, fields, label)
        return self


class PersonaReviewMessages(FrozenPersonaModel):
    """Personality-aware framing around formal structured review data."""

    failure_next_step_title: str = Field(min_length=1, max_length=256)
    escalation_notice: str = Field(min_length=1, max_length=500)
    approved_title: str = Field(min_length=1, max_length=256)
    corrections_required_title: str = Field(min_length=1, max_length=256)
    inconclusive_title: str = Field(min_length=1, max_length=256)
    manual_review_title: str = Field(min_length=1, max_length=256)
    continuation_title: str = Field(min_length=1, max_length=256)
    evidence_reference: str = Field(min_length=1, max_length=300)
    correction_instruction: str = Field(min_length=1, max_length=500)
    charge_supported: str = Field(min_length=1, max_length=150)
    charge_unsupported: str = Field(min_length=1, max_length=150)
    charge_uncertain: str = Field(min_length=1, max_length=150)
    charge_not_found: str = Field(min_length=1, max_length=150)
    charge_review_title: str = Field(min_length=1, max_length=256)
    charge_review_continuation_title: str = Field(min_length=1, max_length=256)
    partial_scope_title: str = Field(min_length=1, max_length=256)
    partial_approved_label: str = Field(min_length=1, max_length=150)
    partial_corrections_required_label: str = Field(min_length=1, max_length=150)
    partial_inconclusive_label: str = Field(min_length=1, max_length=150)
    narrative_scope_label: str = Field(min_length=1, max_length=100)
    charges_scope_label: str = Field(min_length=1, max_length=100)
    targeted_scope_label: str = Field(min_length=1, max_length=100)
    targeted_focus: str = Field(min_length=1, max_length=300)
    fallback_thread_title: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_templates(self) -> PersonaReviewMessages:
        templates = (
            (self.escalation_notice, {"roles"}, "interface.review.escalation_notice"),
            (self.continuation_title, {"display_name"}, "interface.review.continuation_title"),
            (self.evidence_reference, {"evidence"}, "interface.review.evidence_reference"),
            (
                self.partial_scope_title,
                {"verdict", "scope"},
                "interface.review.partial_scope_title",
            ),
            (self.targeted_focus, {"focus"}, "interface.review.targeted_focus"),
            (
                self.correction_instruction,
                {"recommendation"},
                "interface.review.correction_instruction",
            ),
            (self.fallback_thread_title, {"message_id"}, "interface.review.fallback_thread_title"),
        )
        for template, fields, label in templates:
            _require_template_fields(template, fields, label)
        return self


class PersonaCommandMessages(FrozenPersonaModel):
    """Runtime descriptions and compact copy for root slash commands."""

    help_description: str = Field(min_length=1, max_length=100)
    guide_description: str = Field(min_length=1, max_length=100)
    charges_description: str = Field(min_length=1, max_length=100)
    faq_description: str = Field(min_length=1, max_length=100)
    pending_description: str = Field(min_length=1, max_length=100)
    ranking_description: str = Field(min_length=1, max_length=100)
    shame_description: str = Field(min_length=1, max_length=100)
    honor_description: str = Field(min_length=1, max_length=100)
    report_errors_description: str = Field(min_length=1, max_length=100)
    personal_reports_description: str = Field(min_length=1, max_length=100)
    personal_summary_description: str = Field(min_length=1, max_length=100)
    request_roles_description: str = Field(min_length=1, max_length=100)
    info_roles_description: str = Field(min_length=1, max_length=100)
    teamspeak_description: str = Field(min_length=1, max_length=100)
    teamspeak_message: str = Field(min_length=1, max_length=300)
    teamspeak_unauthorized_title: str = Field(min_length=1, max_length=256)
    teamspeak_unauthorized: str = Field(min_length=1, max_length=400)
    pager_description: str = Field(min_length=1, max_length=100)
    active_pagers_description: str = Field(min_length=1, max_length=100)
    my_pagers_description: str = Field(min_length=1, max_length=100)
    info_pager_description: str = Field(min_length=1, max_length=100)
    callsign_description: str = Field(min_length=1, max_length=100)
    remove_callsign_description: str = Field(min_length=1, max_length=100)
    callsigns_description: str = Field(min_length=1, max_length=100)
    info_callsigns_description: str = Field(min_length=1, max_length=100)
    suggestion_description: str = Field(min_length=1, max_length=100)
    info_suggestions_description: str = Field(min_length=1, max_length=100)
    suggestions_description: str = Field(min_length=1, max_length=100)
    suggestion_status_description: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_templates(self) -> PersonaCommandMessages:
        _require_template_fields(
            self.teamspeak_message,
            {"address"},
            "interface.commands.teamspeak_message",
        )
        return self


class PersonaInterfaceMessages(FrozenPersonaModel):
    """Complete deterministic Discord interface copy for one persona."""

    navigation: PersonaNavigationMessages
    knowledge: PersonaKnowledgeMessages
    personal_reports: PersonaPersonalReportMessages
    supervision: PersonaSupervisionMessages
    role_requests: PersonaRoleRequestMessages
    pagers: PersonaPagerMessages
    callsigns: PersonaCallsignMessages
    deployments: PersonaDeploymentMessages
    suggestions: PersonaSuggestionMessages
    review: PersonaReviewMessages
    commands: PersonaCommandMessages


class PersonaMessages(FrozenPersonaModel):
    """Complete deterministic copy selected with the runtime persona."""

    access: PersonaAccessMessages
    failures: PersonaFailureMessages
    help: PersonaHelpMessages
    interface: PersonaInterfaceMessages


class BotPersona(FrozenPersonaModel):
    """Versioned persona contract independent from its storage provider."""

    schema_version: int = Field(ge=1)
    id: str = Field(pattern=PERSONA_ID_PATTERN.pattern)
    revision: int = Field(ge=1)
    discord: DiscordPersona
    identity: PersonaIdentity
    personality: PersonaVoice
    presence: PersonaPresence
    messages: PersonaMessages

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: int) -> int:
        if value != PERSONA_SCHEMA_VERSION:
            raise ValueError(f"unsupported persona schema version {value}; expected {PERSONA_SCHEMA_VERSION}")
        return value


def _require_template_fields(template: str, expected: set[str], label: str) -> None:
    fields: set[str] = set()
    try:
        parts = Formatter().parse(template)
        for _, field_name, format_spec, conversion in parts:
            if field_name is None:
                continue
            if (
                not field_name
                or format_spec
                or conversion
                or any(token in field_name for token in (".", "[", "]"))
            ):
                raise ValueError(f"persona message template '{label}' uses unsafe formatting")
            fields.add(field_name)
    except ValueError as error:
        raise ValueError(f"invalid persona message template '{label}': {error}") from error
    if fields != expected:
        raise ValueError(f"persona message template '{label}' must use placeholders {sorted(expected)}")


@dataclass(frozen=True, slots=True)
class LoadedPersona:
    """A validated persona and its already-loaded avatar."""

    profile: BotPersona
    avatar_bytes: bytes


class PersonaProvider(Protocol):
    """Storage-independent provider that can later be backed by a database."""

    async def load(self, persona_id: str) -> LoadedPersona:
        """Load and validate one complete persona by stable identifier."""


class PackagedPersonaProvider:
    """Load personas and their avatars from package resources."""

    def __init__(self, root: Traversable | None = None) -> None:
        self._root = root or files("arrest_report_agent").joinpath("config", "personas")

    async def load(self, persona_id: str) -> LoadedPersona:
        normalized_id = persona_id.strip()
        if not PERSONA_ID_PATTERN.fullmatch(normalized_id):
            raise PersonaConfigurationError(
                "BOT_PERSONA must use lowercase letters, numbers and single underscores"
            )

        resource = self._root.joinpath(f"{normalized_id}.json")
        try:
            raw_profile = json.loads(resource.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise PersonaConfigurationError(f"unknown BOT_PERSONA '{normalized_id}'") from error
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise PersonaConfigurationError(f"unable to read persona '{normalized_id}': {error}") from error

        try:
            profile = BotPersona.model_validate(raw_profile)
        except Exception as error:
            raise PersonaConfigurationError(f"invalid persona '{normalized_id}': {error}") from error
        if profile.id != normalized_id:
            raise PersonaConfigurationError(
                f"persona id '{profile.id}' does not match BOT_PERSONA '{normalized_id}'"
            )

        avatar_bytes = self._load_asset(profile.discord.avatar, label="avatar")
        return LoadedPersona(
            profile=profile,
            avatar_bytes=avatar_bytes,
        )

    def _load_asset(self, reference: str, *, label: str) -> bytes:
        path = PurePosixPath(reference)
        if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
            raise PersonaConfigurationError(f"persona {label} path must remain inside its profile")
        if path.suffix.lower() not in SUPPORTED_PROFILE_ASSET_SUFFIXES:
            raise PersonaConfigurationError(f"persona {label} must be a PNG, JPEG or GIF image")

        resource = self._root.joinpath(*path.parts)
        try:
            payload = resource.read_bytes()
        except (FileNotFoundError, OSError) as error:
            raise PersonaConfigurationError(f"persona {label} asset '{reference}' is unavailable") from error
        if not payload:
            raise PersonaConfigurationError(f"persona {label} asset cannot be empty")
        if len(payload) > MAX_PROFILE_ASSET_BYTES:
            raise PersonaConfigurationError(f"persona {label} asset exceeds {MAX_PROFILE_ASSET_BYTES} bytes")
        if not _matches_image_signature(payload, path.suffix.lower()):
            raise PersonaConfigurationError(
                f"persona {label} asset '{reference}' does not match its image format"
            )
        return payload


def _matches_image_signature(payload: bytes, suffix: str) -> bool:
    if suffix == ".png":
        return payload.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {".jpg", ".jpeg"}:
        return payload.startswith(b"\xff\xd8\xff")
    if suffix == ".gif":
        return payload.startswith((b"GIF87a", b"GIF89a"))
    return False
