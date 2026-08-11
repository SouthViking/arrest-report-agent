"""Immutable configuration snapshots resolved from PostgreSQL."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .settings import AppSettings, SnowflakeAllowlist


class FrozenConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AccessPolicyMode(StrEnum):
    OPEN = "open"
    RESTRICTED = "restricted"
    DISABLED = "disabled"


class AccessPolicyKey(StrEnum):
    REPORT_CHANNELS = "report_channels"
    AGENT_USERS = "agent_users"
    SUPERVISOR_ROLES = "supervisor_roles"
    REVIEW_ESCALATION_ROLES = "review_escalation_roles"
    ROLE_REQUEST_CHANNELS = "role_request_channels"
    ROLE_REQUEST_ALLOWED_ROLES = "role_request_allowed_roles"
    ROLE_REQUEST_REVIEWER_ROLES = "role_request_reviewer_roles"
    CONFIG_ADMIN_ROLES = "config_admin_roles"


POLICY_SUBJECT_TYPES: dict[AccessPolicyKey, Literal["channel", "role", "user"]] = {
    AccessPolicyKey.REPORT_CHANNELS: "channel",
    AccessPolicyKey.AGENT_USERS: "user",
    AccessPolicyKey.SUPERVISOR_ROLES: "role",
    AccessPolicyKey.REVIEW_ESCALATION_ROLES: "role",
    AccessPolicyKey.ROLE_REQUEST_CHANNELS: "channel",
    AccessPolicyKey.ROLE_REQUEST_ALLOWED_ROLES: "role",
    AccessPolicyKey.ROLE_REQUEST_REVIEWER_ROLES: "role",
    AccessPolicyKey.CONFIG_ADMIN_ROLES: "role",
}


class AccessPolicySnapshot(FrozenConfigModel):
    key: AccessPolicyKey
    subject_type: Literal["channel", "role", "user"]
    mode: AccessPolicyMode
    ids: tuple[int, ...] = ()

    @model_validator(mode="after")
    def validate_policy(self) -> AccessPolicySnapshot:
        expected_type = POLICY_SUBJECT_TYPES[self.key]
        if self.subject_type != expected_type:
            raise ValueError(f"{self.key.value} requires {expected_type} subjects")
        if self.mode is AccessPolicyMode.RESTRICTED and not self.ids:
            raise ValueError(f"restricted policy {self.key.value} requires at least one ID")
        if self.mode is not AccessPolicyMode.RESTRICTED and self.ids:
            raise ValueError(f"{self.mode.value} policy {self.key.value} cannot contain IDs")
        if any(value <= 0 for value in self.ids) or len(set(self.ids)) != len(self.ids):
            raise ValueError(f"policy {self.key.value} contains invalid or duplicate IDs")
        return self

    def as_allowlist(self) -> SnowflakeAllowlist:
        if self.mode is AccessPolicyMode.OPEN:
            return SnowflakeAllowlist(ids=None)
        return SnowflakeAllowlist(ids=frozenset(self.ids))


class GuildAgentConfig(FrozenConfigModel):
    model: str = Field(min_length=1, max_length=100)
    reasoning_effort: str = Field(min_length=1, max_length=30)
    review_timeout_seconds: float = Field(gt=0, le=600)
    max_agent_turns: int = Field(ge=2, le=50)
    max_images_per_review: int = Field(ge=1, le=30)
    max_image_bytes: int = Field(ge=1)
    max_files_per_review: int = Field(ge=1, le=10)
    max_file_bytes: int = Field(ge=1)
    max_total_file_bytes: int = Field(ge=1, le=52_428_800)
    thread_context_max_messages: int = Field(ge=1, le=100)
    channel_context_max_messages: int = Field(ge=0, le=25)
    thread_context_max_chars: int = Field(ge=500)
    thread_context_max_message_chars: int = Field(ge=100)
    thread_context_max_historical_images: int = Field(ge=0, le=10)
    thread_context_max_historical_files: int = Field(ge=0, le=5)
    thread_auto_archive_minutes: Literal[60, 1440, 4320, 10080]


class GuildFeatureSnapshot(FrozenConfigModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,79}$")
    enabled: bool
    schema_version: int = Field(ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)


class GuildConfigSnapshot(FrozenConfigModel):
    guild_id: int = Field(gt=0)
    revision_id: UUID
    revision: int = Field(ge=1)
    agent: GuildAgentConfig
    policies: dict[AccessPolicyKey, AccessPolicySnapshot]
    features: dict[str, GuildFeatureSnapshot]
    persona_revision_id: UUID
    persona_id: str = Field(pattern=r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
    persona_revision: int = Field(ge=1)

    @model_validator(mode="after")
    def require_policies(self) -> GuildConfigSnapshot:
        missing = set(AccessPolicyKey) - set(self.policies)
        if missing:
            raise ValueError(
                "guild configuration is missing policies: "
                + ", ".join(sorted(item.value for item in missing))
            )
        return self

    def policy(self, key: AccessPolicyKey) -> AccessPolicySnapshot:
        return self.policies[key]


class GuildRuntimeSettings:
    """Settings-shaped view used by existing deterministic adapters."""

    def __init__(self, process: AppSettings, snapshot: GuildConfigSnapshot) -> None:
        self.process = process
        self.snapshot = snapshot

    def __getattr__(self, name: str) -> Any:
        if hasattr(self.snapshot.agent, name):
            return getattr(self.snapshot.agent, name)
        return getattr(self.process, name)

    @property
    def guild_allowlist(self) -> SnowflakeAllowlist:
        return SnowflakeAllowlist(ids=frozenset({self.snapshot.guild_id}))

    @property
    def report_channel_allowlist(self) -> SnowflakeAllowlist:
        return self.snapshot.policy(AccessPolicyKey.REPORT_CHANNELS).as_allowlist()

    @property
    def user_allowlist(self) -> SnowflakeAllowlist:
        return self.snapshot.policy(AccessPolicyKey.AGENT_USERS).as_allowlist()

    @property
    def supervisor_role_allowlist(self) -> SnowflakeAllowlist:
        return self.snapshot.policy(AccessPolicyKey.SUPERVISOR_ROLES).as_allowlist()

    @property
    def review_escalation_role_ids(self) -> tuple[int, ...]:
        policy = self.snapshot.policy(AccessPolicyKey.REVIEW_ESCALATION_ROLES)
        return policy.ids if policy.mode is AccessPolicyMode.RESTRICTED else ()

    @property
    def role_request_allowed_channel_allowlist(self) -> SnowflakeAllowlist:
        return self.snapshot.policy(AccessPolicyKey.ROLE_REQUEST_CHANNELS).as_allowlist()

    @property
    def role_request_allowed_role_allowlist(self) -> SnowflakeAllowlist:
        return self.snapshot.policy(AccessPolicyKey.ROLE_REQUEST_ALLOWED_ROLES).as_allowlist()

    @property
    def role_request_allowed_role_ids(self) -> tuple[int, ...]:
        policy = self.snapshot.policy(AccessPolicyKey.ROLE_REQUEST_ALLOWED_ROLES)
        return policy.ids if policy.mode is AccessPolicyMode.RESTRICTED else ()

    @property
    def role_request_reviewer_role_ids(self) -> tuple[int, ...]:
        policy = self.snapshot.policy(AccessPolicyKey.ROLE_REQUEST_REVIEWER_ROLES)
        return policy.ids if policy.mode is AccessPolicyMode.RESTRICTED else ()

    @property
    def openai_model(self) -> str:
        return self.snapshot.agent.model

    @property
    def openai_reasoning_effort(self) -> str:
        return self.snapshot.agent.reasoning_effort

    @property
    def max_images_per_review(self) -> int:
        return min(self.snapshot.agent.max_images_per_review, self.process.max_images_per_review)

    @property
    def max_image_bytes(self) -> int:
        return min(self.snapshot.agent.max_image_bytes, self.process.max_image_bytes)

    @property
    def max_files_per_review(self) -> int:
        return min(self.snapshot.agent.max_files_per_review, self.process.max_files_per_review)

    @property
    def max_file_bytes(self) -> int:
        return min(self.snapshot.agent.max_file_bytes, self.process.max_file_bytes)

    @property
    def max_total_file_bytes(self) -> int:
        return min(self.snapshot.agent.max_total_file_bytes, self.process.max_total_file_bytes)

    @property
    def teamspeak_address(self) -> str:
        feature = self.snapshot.features.get("teamspeak")
        return str((feature.payload if feature else {}).get("address", "51.81.49.72:11189"))
