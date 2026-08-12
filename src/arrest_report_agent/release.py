"""Packaged, versioned release manifest used by deployment announcements."""

from __future__ import annotations

import json
import re
from enum import StrEnum
from importlib.resources import files

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from arrest_report_agent import __version__


class ReleaseManifestError(RuntimeError):
    """Raised when the packaged release manifest is invalid."""


class ReleaseChangeType(StrEnum):
    ADDED = "added"
    IMPROVED = "improved"
    FIXED = "fixed"
    SECURITY = "security"


class FrozenReleaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ReleaseChange(FrozenReleaseModel):
    type: ReleaseChangeType
    text: str = Field(min_length=1, max_length=300)


class ReleaseManifest(FrozenReleaseModel):
    schema_version: int
    version: str = Field(
        pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$"
    )
    title: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=500)
    repository_url: str = Field(min_length=1, max_length=255)
    changes: tuple[ReleaseChange, ...] = Field(min_length=1, max_length=10)

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
    def validate_repository_url(cls, value: str) -> str:
        if re.fullmatch(
            r"https://github\.com/[A-Za-z0-9.-]+/[A-Za-z0-9_.-]+",
            value,
        ) is None:
            raise ValueError("repository_url must identify a GitHub repository")
        return value

    @model_validator(mode="after")
    def validate_contract(self) -> ReleaseManifest:
        if self.schema_version != 1:
            raise ValueError("unsupported release manifest schema version")
        if self.version != __version__:
            raise ValueError(
                f"release manifest version {self.version} does not match app version {__version__}"
            )
        return self

    @classmethod
    def load_packaged(cls) -> ReleaseManifest:
        try:
            resource = files("arrest_report_agent").joinpath("release.json")
            payload = json.loads(resource.read_text(encoding="utf-8"))
            return cls.model_validate(payload)
        except Exception as error:
            raise ReleaseManifestError("Unable to load the packaged release manifest") from error
