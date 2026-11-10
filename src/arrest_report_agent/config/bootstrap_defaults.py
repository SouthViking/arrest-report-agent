"""Typed guild-specific defaults used only during the initial database import."""

from __future__ import annotations

from dataclasses import dataclass

from .bootstrap_profile import load_bootstrap_profile
from .database import AccessPolicyKey


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
    supervisor_extra_notification_role_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class PendingArrestSupervisorAlertDefaults:
    """Initial supervisor escalation, editable in the published guild configuration."""

    channel_ids: tuple[int, ...]
    role_id: int


def role_request_review_defaults(guild_id: int) -> RoleRequestReviewDefaults | None:
    profile = load_bootstrap_profile().guilds.get(guild_id)
    if profile is None:
        return None
    policies = {item.key: item for item in profile.policy_snapshots()}
    return RoleRequestReviewDefaults(
        channel_ids=policies[AccessPolicyKey.ROLE_REQUEST_REVIEW_CHANNELS].ids,
        reviewer_role_ids=policies[AccessPolicyKey.ROLE_REQUEST_REVIEWER_ROLES].ids,
    )


def ftp_defaults(guild_id: int) -> FtpDefaults | None:
    profile = load_bootstrap_profile().guilds.get(guild_id)
    if profile is None:
        return None
    status = profile.features["ftp_status"].payload
    requests = profile.features["ftp_requests"].payload
    return FtpDefaults(
        faction_id=status["faction_id"],
        supervisory_role_ids=tuple(status["supervisory_role_ids"]),
        mfto_role_ids=tuple(status["mfto_role_ids"]), fto_role_ids=tuple(status["fto_role_ids"]),
        ftt_role_ids=tuple(status["ftt_role_ids"]), request_channel_id=requests["channel_id"],
        command_role_ids=tuple(requests["command_role_ids"]),
        executive_role_ids=tuple(requests["executive_role_ids"]),
        supervisor_extra_notification_role_ids=tuple(requests["supervisor_extra_notification_role_ids"]),
    )


def pending_arrest_supervisor_alert_defaults(guild_id: int) -> PendingArrestSupervisorAlertDefaults | None:
    profile = load_bootstrap_profile().guilds.get(guild_id)
    if profile is None:
        return None
    alerts = profile.features["pending_arrest_reports"].payload
    return PendingArrestSupervisorAlertDefaults(
        channel_ids=tuple(alerts["supervisor_alert_channel_ids"]), role_id=alerts["supervisor_alert_role_id"],
    )
