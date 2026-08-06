"""Shared role policy for deterministic Discord supervision features."""

from __future__ import annotations

from dataclasses import dataclass

import discord

from arrest_report_agent.config import AppSettings, SnowflakeAllowlist


@dataclass(frozen=True, slots=True)
class SupervisorRolePolicy:
    """Central role policy that can later be backed by persisted configuration."""

    roles: SnowflakeAllowlist

    @classmethod
    def from_settings(cls, settings: AppSettings) -> SupervisorRolePolicy:
        return cls(roles=settings.supervisor_role_allowlist)

    @property
    def is_restricted(self) -> bool:
        return self.roles.is_restricted

    def allows(self, member: discord.Member) -> bool:
        if not self.roles.is_restricted:
            return True
        return any(self.roles.allows(role.id) for role in member.roles)
