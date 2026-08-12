"""Validated Railway webhook payloads and canonical deployment event types."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RailwayWebhookPayloadError(ValueError):
    """Raised when a Railway webhook is not a supported deployment event."""


class DeploymentEventType(StrEnum):
    DEPLOYED = "deployed"
    FAILED = "failed"
    CRASHED = "crashed"
    OOM_KILLED = "oom_killed"

    @classmethod
    def from_railway(cls, raw_value: str) -> DeploymentEventType:
        prefix, separator, suffix = raw_value.partition(".")
        if separator != "." or prefix.lower() != "deployment":
            raise RailwayWebhookPayloadError("Unsupported Railway webhook type")
        normalized = re.sub(r"[^a-z]", "", suffix.lower())
        values = {
            "deployed": cls.DEPLOYED,
            "failed": cls.FAILED,
            "crashed": cls.CRASHED,
            "oomkilled": cls.OOM_KILLED,
        }
        try:
            return values[normalized]
        except KeyError as error:
            raise RailwayWebhookPayloadError("Unsupported Railway deployment event") from error


class RailwayModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, str_strip_whitespace=True)


class RailwayEventDetails(RailwayModel):
    id: str | None = Field(default=None, max_length=100)
    source: str | None = Field(default=None, max_length=50)
    status: str | None = Field(default=None, max_length=50)
    branch: str | None = Field(default=None, max_length=255)
    commit_hash: str | None = Field(default=None, alias="commitHash", max_length=64)
    commit_author: str | None = Field(default=None, alias="commitAuthor", max_length=255)
    commit_message: str | None = Field(default=None, alias="commitMessage", max_length=500)


class RailwayResourceIdentity(RailwayModel):
    id: str = Field(min_length=1, max_length=100)
    name: str | None = Field(default=None, max_length=255)


class RailwayEnvironmentIdentity(RailwayResourceIdentity):
    is_ephemeral: bool = Field(default=False, alias="isEphemeral")


class RailwayWebhookResources(RailwayModel):
    workspace: RailwayResourceIdentity | None = None
    project: RailwayResourceIdentity
    environment: RailwayEnvironmentIdentity
    service: RailwayResourceIdentity
    deployment: RailwayResourceIdentity


class RailwayDeploymentEvent(RailwayModel):
    type: str = Field(min_length=1, max_length=100)
    details: RailwayEventDetails = Field(default_factory=RailwayEventDetails)
    resource: RailwayWebhookResources
    severity: str | None = Field(default=None, max_length=30)
    timestamp: datetime

    @field_validator("timestamp")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Railway event timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_supported_event(self) -> RailwayDeploymentEvent:
        _ = self.event_type
        return self

    @property
    def event_type(self) -> DeploymentEventType:
        return DeploymentEventType.from_railway(self.type)

    @property
    def deployment_id(self) -> str:
        return self.resource.deployment.id

    @property
    def provider_event_key(self) -> str:
        value = f"railway:{self.event_type.value}:{self.deployment_id}"
        return hashlib.sha256(value.encode()).hexdigest()

    @property
    def has_verifiable_commit(self) -> bool:
        commit = self.details.commit_hash or ""
        return bool(re.fullmatch(r"[0-9a-fA-F]{7,64}", commit))

    def matches_commit(self, commit_hash: str) -> bool:
        """Compare full or abbreviated Git SHAs without trusting arbitrary text."""

        candidate = self.details.commit_hash or ""
        if not self.has_verifiable_commit or not re.fullmatch(
            r"[0-9a-fA-F]{7,64}", commit_hash
        ):
            return False
        left, right = candidate.lower(), commit_hash.lower()
        return left.startswith(right) or right.startswith(left)

    def matches_runtime(
        self,
        *,
        project_id: str,
        environment_id: str,
        service_id: str,
    ) -> bool:
        return (
            self.resource.project.id == project_id
            and self.resource.environment.id == environment_id
            and not self.resource.environment.is_ephemeral
            and self.resource.service.id == service_id
        )
