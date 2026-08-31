"""Stable identifiers and persistence records for agent conversations."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentConversationKey:
    """Identify one logical conversation without coupling storage to a feature."""

    namespace: str
    scope_key: str
    access_partition: str

    @property
    def session_id(self) -> str:
        raw = "\x1f".join((self.namespace, self.scope_key, self.access_partition))
        return "agent-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AgentSessionRecord:
    session_id: str
    source_cursor: str | None
    created: bool
