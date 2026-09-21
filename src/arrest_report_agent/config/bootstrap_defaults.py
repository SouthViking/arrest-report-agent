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


@dataclass(frozen=True, slots=True)
class FtpDefaults:
    """Initial FTP role catalogue and assistance destination."""

    faction_id: int
    supervisory_role_ids: tuple[int, ...]
    mfto_role_ids: tuple[int, ...]
    fto_role_ids: tuple[int, ...]
    ftt_role_ids: tuple[int, ...]
    request_channel_id: int
    command_role_ids: tuple[int, ...]
    executive_role_ids: tuple[int, ...]


_ROLE_REQUEST_REVIEW_DEFAULTS: Mapping[int, RoleRequestReviewDefaults] = MappingProxyType(
    {
        1234674521416663070: RoleRequestReviewDefaults(
            channel_ids=(1261076248541532240,),
            reviewer_role_ids=(
                1234678029662294047,
                1234678238182244414,
            ),
        ),
    }
)

_FTP_DEFAULTS: Mapping[int, FtpDefaults] = MappingProxyType(
    {
        1234674521416663070: FtpDefaults(
            faction_id=3,
            supervisory_role_ids=(1234676131240869949,),
            mfto_role_ids=(1234691474055233607,),
            fto_role_ids=(1234675643959083021,),
            ftt_role_ids=(1234675742013390849,),
            request_channel_id=1551692208929906701,
            command_role_ids=(1234678029662294047,),
            executive_role_ids=(1234678238182244414,),
        ),
    }
)


def role_request_review_defaults(guild_id: int) -> RoleRequestReviewDefaults | None:
    """Return immutable initial defaults without affecting runtime database policies."""

    return _ROLE_REQUEST_REVIEW_DEFAULTS.get(guild_id)


def ftp_defaults(guild_id: int) -> FtpDefaults | None:
    """Return immutable initial FTP defaults without affecting runtime snapshots."""

    return _FTP_DEFAULTS.get(guild_id)
