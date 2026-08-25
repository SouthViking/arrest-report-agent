"""Typed guild-specific defaults used only during the initial database import."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class RoleRequestReviewDefaults:
    """Initial private-review destinations and authorized reviewer roles."""

    channel_ids: tuple[int, ...]
    reviewer_role_ids: tuple[int, ...]


_ROLE_REQUEST_REVIEW_DEFAULTS: Mapping[int, RoleRequestReviewDefaults] = MappingProxyType(
    {
        1234674521416663070: RoleRequestReviewDefaults(
            channel_ids=(1261076248541532240,),
            reviewer_role_ids=(1234678029662294047,),
        ),
    }
)


def role_request_review_defaults(guild_id: int) -> RoleRequestReviewDefaults | None:
    """Return immutable initial defaults without affecting runtime database policies."""

    return _ROLE_REQUEST_REVIEW_DEFAULTS.get(guild_id)
