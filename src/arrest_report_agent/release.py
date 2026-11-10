"""Packaged, versioned release manifest used by deployment announcements."""

from __future__ import annotations

import json
import re
from enum import StrEnum
from importlib.resources import files
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from arrest_report_agent import __version__


class ReleaseManifestError(RuntimeError):
    """Raised when the packaged release manifest is invalid."""


class ReleaseChangeType(StrEnum):
    ADDED = "added"
    IMPROVED = "improved"
    FIXED = "fixed"
    SECURITY = "security"


class ReleaseAcknowledgementType(StrEnum):
    SUGGESTION = "suggestion"
    TESTING = "testing"
    CONTRIBUTION = "contribution"


class FrozenReleaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ReleaseChange(FrozenReleaseModel):
    type: ReleaseChangeType
    text: str = Field(min_length=1, max_length=300)


class ReleaseAcknowledgement(FrozenReleaseModel):
    type: ReleaseAcknowledgementType
    user_id: int = Field(gt=0, le=2**64 - 1)
    text: str = Field(min_length=1, max_length=150)


class ReleaseManifest(FrozenReleaseModel):
    schema_version: int
    version: str = Field(
        pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$"
    )
    title: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=500)
    repository_url: str | None = Field(default=None, min_length=1, max_length=255)
    changelog_targets: Literal["all", "internal"] | tuple[int, ...] = "all"
    changes: tuple[ReleaseChange, ...] = Field(min_length=1, max_length=10)
    acknowledgements: tuple[ReleaseAcknowledgement, ...] = Field(
        default=(),
        max_length=5,
    )

    @field_validator("changelog_targets", mode="before")
    @classmethod
    def validate_changelog_targets(
        cls,
        value: object,
    ) -> Literal["all", "internal"] | tuple[int, ...]:
        if value in ("all", "internal"):
            return "internal" if value == "internal" else "all"
        if not isinstance(value, (list, tuple)):
            raise ValueError("changelog_targets must be 'all', 'internal' or a list of channel IDs")
        if any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in value):
            raise ValueError("changelog_targets must contain positive Discord channel IDs")
        targets = tuple(value)
        if len(set(targets)) != len(targets):
            raise ValueError("changelog_targets cannot contain duplicate channel IDs")
        return targets

    @field_validator("version")
    @classmethod
    def validate_semver(cls, value: str) -> str:
        parts = value.split(".")
        if len(parts) != 3 or any(not part.isdigit() for part in parts):
            raise ValueError("version must use semantic versioning (MAJOR.MINOR.PATCH)")
        if any(len(part) > 1 and part.startswith("0") for part in parts):
            raise ValueError("semantic version components cannot have leading zeroes")
        return value

    @field_validator("repository_url")
    @classmethod
    def validate_repository_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if re.fullmatch(
            r"https://github\.com/[A-Za-z0-9.-]+/[A-Za-z0-9_.-]+",
            value,
        ) is None:
            raise ValueError("repository_url must identify a GitHub repository")
        return value

    @model_validator(mode="after")
    def validate_contract(self) -> ReleaseManifest:
        if self.schema_version != 3:
            raise ValueError("unsupported release manifest schema version")
        if self.version != __version__:
            raise ValueError(
                f"release manifest version {self.version} does not match app version {__version__}"
            )
        return self

    def targets_changelog_channel(
        self, channel_id: int, *, internal_channel_ids: tuple[int, ...] = (),
    ) -> bool:
        if self.changelog_targets == "internal":
            return channel_id in internal_channel_ids
        return self.changelog_targets == "all" or channel_id in self.changelog_targets

    @property
    def repository_full_name(self) -> str | None:
        return self.repository_url.removeprefix("https://github.com/") if self.repository_url else None

    @classmethod
    def load_packaged(cls, *, repository_url: str | None = None) -> ReleaseManifest:
        try:
            resource = files("arrest_report_agent").joinpath("release.json")
            payload = json.loads(resource.read_text(encoding="utf-8"))
            if repository_url is not None:
                payload["repository_url"] = repository_url
            return cls.model_validate(payload)
        except Exception as error:
            raise ReleaseManifestError("Unable to load the packaged release manifest") from error
