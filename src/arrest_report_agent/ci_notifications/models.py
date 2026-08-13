"""Validated GitHub Actions workflow-run webhook payloads."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class GitHubWebhookPayloadError(ValueError):
    """Raised when a GitHub delivery is malformed or outside the supported contract."""


class GitHubModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, str_strip_whitespace=True)


class GitHubActor(GitHubModel):
    login: str = Field(min_length=1, max_length=100)


class GitHubRepository(GitHubModel):
    id: int = Field(gt=0)
    full_name: str = Field(min_length=3, max_length=255)

    @field_validator("full_name")
    @classmethod
    def validate_full_name(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value) is None:
            raise ValueError("invalid GitHub repository name")
        return value


class GitHubHeadCommit(GitHubModel):
    message: str | None = Field(default=None, max_length=500)


class GitHubWorkflowRun(GitHubModel):
    id: int = Field(gt=0)
    workflow_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=255)
    event: str = Field(min_length=1, max_length=50)
    status: str = Field(min_length=1, max_length=30)
    conclusion: str | None = Field(default=None, max_length=30)
    run_attempt: int = Field(default=1, ge=1)
    head_branch: str | None = Field(default=None, max_length=255)
    head_sha: str = Field(min_length=7, max_length=64)
    actor: GitHubActor
    head_commit: GitHubHeadCommit | None = None
    run_started_at: datetime | None = None
    updated_at: datetime

    @field_validator("head_sha")
    @classmethod
    def validate_sha(cls, value: str) -> str:
        if re.fullmatch(r"[0-9a-fA-F]{7,64}", value) is None:
            raise ValueError("invalid Git commit SHA")
        return value.lower()

    @field_validator("run_started_at", "updated_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("GitHub timestamps must include a timezone")
        return value.astimezone(UTC)


class GitHubWorkflowRunEvent(GitHubModel):
    action: str = Field(min_length=1, max_length=30)
    repository: GitHubRepository
    sender: GitHubActor
    workflow_run: GitHubWorkflowRun

    def matches_repository(self, expected: str) -> bool:
        return self.repository.full_name.casefold() == expected.casefold()

    @property
    def provider_event_key(self) -> str:
        value = (
            f"github-actions:{self.repository.id}:{self.workflow_run.id}:"
            f"{self.workflow_run.run_attempt}:failure"
        )
        return hashlib.sha256(value.encode()).hexdigest()
