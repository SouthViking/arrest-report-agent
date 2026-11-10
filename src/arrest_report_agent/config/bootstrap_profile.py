"""Declarative installation presets; published configuration remains in PostgreSQL."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .database import AccessPolicyKey, AccessPolicySnapshot, GuildFeatureSnapshot


class ProfileFeature(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool
    schema_version: int = Field(default=1, ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    requires_supervisor_roles: bool = False

    def snapshot(self, key: str, *, has_supervisor_roles: bool) -> GuildFeatureSnapshot:
        return GuildFeatureSnapshot(
            key=key,
            enabled=self.enabled and (not self.requires_supervisor_roles or has_supervisor_roles),
            schema_version=self.schema_version,
            payload=self.payload,
        )


class GuildBootstrapProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    policies: dict[AccessPolicyKey, dict[str, Any]] = Field(default_factory=dict)
    features: dict[str, ProfileFeature] = Field(default_factory=dict)

    def policy_snapshots(self) -> tuple[AccessPolicySnapshot, ...]:
        return tuple(
            AccessPolicySnapshot.model_validate({**payload, "key": key})
            for key, payload in self.policies.items()
        )


class BootstrapProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: int = Field(default=1, ge=1, le=1)
    guilds: dict[int, GuildBootstrapProfile] = Field(default_factory=dict)


def load_bootstrap_profile(path: str | None = None) -> BootstrapProfile:
    """An explicit 'none' selects a clean installation without institutional defaults."""
    if path == "none":
        return BootstrapProfile()
    if path:
        raw = Path(path).read_text(encoding="utf-8")
    else:
        raw = (
            files("arrest_report_agent")
            .joinpath("config/data/bootstrap_profile.json")
            .read_text(
                encoding="utf-8",
            )
        )
    profile = BootstrapProfile.model_validate(json.loads(raw))
    for guild in profile.guilds.values():
        guild.policy_snapshots()
    return profile
