"""Immutable configuration snapshots resolved from PostgreSQL."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from .settings import AppSettings, SnowflakeAllowlist


class FrozenConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AccessPolicyMode(StrEnum):
    OPEN = "open"
    RESTRICTED = "restricted"
    DISABLED = "disabled"


class ArrestReportAttachmentMode(StrEnum):
    """How structured submissions provide the visible arrest-report copy."""

    MANUAL = "manual"
    GENERATED = "generated"


class AccessPolicyKey(StrEnum):
    REPORT_CHANNELS = "report_channels"
    AGENT_USERS = "agent_users"
    AGENT_ROLES = "agent_roles"
    AGENT_BLOCKED_USERS = "agent_blocked_users"
    SUPERVISOR_ROLES = "supervisor_roles"
    REPORT_ANALYTICS_ROLES = "report_analytics_roles"
    REVIEW_ESCALATION_ROLES = "review_escalation_roles"
    ROLE_REQUEST_CHANNELS = "role_request_channels"
    ROLE_REQUEST_REVIEW_CHANNELS = "role_request_review_channels"
    ROLE_REQUEST_ALLOWED_ROLES = "role_request_allowed_roles"
    ROLE_REQUEST_REVIEWER_ROLES = "role_request_reviewer_roles"
    CONFIG_ADMIN_ROLES = "config_admin_roles"
    PAGER_CHANNELS = "pager_channels"
    PAGER_CREATOR_ROLES = "pager_creator_roles"
    PAGER_RECIPIENT_ROLES = "pager_recipient_roles"
    PAGER_CLOSER_ROLES = "pager_closer_roles"
    OPERATIONAL_STAFF_ROLES = "operational_staff_roles"
    RADIO_AUDIT_ROLES = "radio_audit_roles"
    FACTION_REPORT_ROLES = "faction_report_roles"
    DEPLOYMENT_NOTIFICATION_CHANNELS = "deployment_notification_channels"
    DEPLOYMENT_INCIDENT_NOTIFICATION_CHANNELS = "deployment_incident_notification_channels"
    CI_FAILURE_NOTIFICATION_CHANNELS = "ci_failure_notification_channels"
    SERVICE_CALL_CHANNELS = "service_call_channels"
    STOLEN_VEHICLE_CHANNELS = "stolen_vehicle_channels"
    LOGBOOK_CHANNELS = "logbook_channels"


POLICY_SUBJECT_TYPES: dict[AccessPolicyKey, Literal["channel", "role", "user"]] = {
    AccessPolicyKey.REPORT_CHANNELS: "channel",
    AccessPolicyKey.AGENT_USERS: "user",
    AccessPolicyKey.AGENT_ROLES: "role",
    AccessPolicyKey.AGENT_BLOCKED_USERS: "user",
    AccessPolicyKey.SUPERVISOR_ROLES: "role",
    AccessPolicyKey.REPORT_ANALYTICS_ROLES: "role",
    AccessPolicyKey.REVIEW_ESCALATION_ROLES: "role",
    AccessPolicyKey.ROLE_REQUEST_CHANNELS: "channel",
    AccessPolicyKey.ROLE_REQUEST_REVIEW_CHANNELS: "channel",
    AccessPolicyKey.ROLE_REQUEST_ALLOWED_ROLES: "role",
    AccessPolicyKey.ROLE_REQUEST_REVIEWER_ROLES: "role",
    AccessPolicyKey.CONFIG_ADMIN_ROLES: "role",
    AccessPolicyKey.PAGER_CHANNELS: "channel",
    AccessPolicyKey.PAGER_CREATOR_ROLES: "role",
    AccessPolicyKey.PAGER_RECIPIENT_ROLES: "role",
    AccessPolicyKey.PAGER_CLOSER_ROLES: "role",
    AccessPolicyKey.OPERATIONAL_STAFF_ROLES: "role",
    AccessPolicyKey.RADIO_AUDIT_ROLES: "role",
    AccessPolicyKey.FACTION_REPORT_ROLES: "role",
    AccessPolicyKey.DEPLOYMENT_NOTIFICATION_CHANNELS: "channel",
    AccessPolicyKey.DEPLOYMENT_INCIDENT_NOTIFICATION_CHANNELS: "channel",
    AccessPolicyKey.CI_FAILURE_NOTIFICATION_CHANNELS: "channel",
    AccessPolicyKey.SERVICE_CALL_CHANNELS: "channel",
    AccessPolicyKey.STOLEN_VEHICLE_CHANNELS: "channel",
    AccessPolicyKey.LOGBOOK_CHANNELS: "channel",
}


class AccessPolicySnapshot(FrozenConfigModel):
    key: AccessPolicyKey
    subject_type: Literal["channel", "role", "user"]
    mode: AccessPolicyMode
    ids: tuple[int, ...] = ()

    @model_validator(mode="after")
    def validate_policy(self) -> AccessPolicySnapshot:
        expected_type = POLICY_SUBJECT_TYPES[self.key]
        if self.subject_type != expected_type:
            raise ValueError(f"{self.key.value} requires {expected_type} subjects")
        if self.mode is AccessPolicyMode.RESTRICTED and not self.ids:
            raise ValueError(f"restricted policy {self.key.value} requires at least one ID")
        if self.key is AccessPolicyKey.AGENT_BLOCKED_USERS and self.mode is AccessPolicyMode.OPEN:
            raise ValueError("agent_blocked_users only supports explicit IDs or disabled mode")
        if self.mode is not AccessPolicyMode.RESTRICTED and self.ids:
            raise ValueError(f"{self.mode.value} policy {self.key.value} cannot contain IDs")
        if any(value <= 0 for value in self.ids) or len(set(self.ids)) != len(self.ids):
            raise ValueError(f"policy {self.key.value} contains invalid or duplicate IDs")
        return self

    def as_allowlist(self) -> SnowflakeAllowlist:
        if self.mode is AccessPolicyMode.OPEN:
            return SnowflakeAllowlist(ids=None)
        return SnowflakeAllowlist(ids=frozenset(self.ids))


class GuildAgentConfig(FrozenConfigModel):
    model: str = Field(min_length=1, max_length=100)
    reasoning_effort: str = Field(min_length=1, max_length=30)
    review_timeout_seconds: float = Field(gt=0, le=600)
    max_agent_turns: int = Field(ge=2, le=50)
    max_images_per_review: int = Field(ge=1, le=30)
    max_image_bytes: int = Field(ge=1)
    max_files_per_review: int = Field(ge=1, le=10)
    max_file_bytes: int = Field(ge=1)
    max_total_file_bytes: int = Field(ge=1, le=52_428_800)
    thread_context_max_messages: int = Field(ge=1, le=100)
    channel_context_max_messages: int = Field(ge=0, le=25)
    thread_context_max_chars: int = Field(ge=500)
    thread_context_max_message_chars: int = Field(ge=100)
    thread_context_max_historical_images: int = Field(ge=0, le=10)
    thread_context_max_historical_files: int = Field(ge=0, le=5)
    thread_auto_archive_minutes: Literal[60, 1440, 4320, 10080]


class GuildFeatureSnapshot(FrozenConfigModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,79}$")
    enabled: bool
    schema_version: int = Field(ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)


class ReportOwnerAccessOptions(FrozenConfigModel):
    max_rounds: int = Field(ge=1, le=20)
    quota_grant_role_ids: tuple[int, ...] = Field(default=(), max_length=10)
    quota_exhausted_notification_channel_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_quota_grant_roles(self) -> ReportOwnerAccessOptions:
        if any(role_id <= 0 for role_id in self.quota_grant_role_ids):
            raise ValueError("quota grant role IDs must be positive")
        if len(self.quota_grant_role_ids) != len(set(self.quota_grant_role_ids)):
            raise ValueError("quota grant role IDs must be unique")
        return self


class ReviewFailureNotificationOptions(FrozenConfigModel):
    """Explicit recipients for an exhausted automatic review recovery."""

    role_ids: tuple[Annotated[int, Field(gt=0, le=2**64 - 1, strict=True)], ...] = Field(
        default=(), max_length=25
    )
    user_ids: tuple[Annotated[int, Field(gt=0, le=2**64 - 1, strict=True)], ...] = Field(
        default=(), max_length=25
    )

    @model_validator(mode="after")
    def validate_recipients(self) -> ReviewFailureNotificationOptions:
        if len(self.role_ids) != len(set(self.role_ids)) or len(self.user_ids) != len(set(self.user_ids)):
            raise ValueError("review failure notification IDs must be unique within each whitelist")
        return self


class RoleRequestStationOption(FrozenConfigModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,39}$")
    label: str = Field(min_length=1, max_length=100)
    role_id: int = Field(gt=0)
    emoji: str | None = Field(default=None, min_length=1, max_length=20)


class RoleRequestFttWelcomeOptions(FrozenConfigModel):
    channel_id: int = Field(gt=0)
    rules_url: HttpUrl
    reference_channel_ids: tuple[int, ...] = Field(min_length=1, max_length=5)
    information_channel_id: int | None = Field(default=None, gt=0)
    questions_channel_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_channels(self) -> RoleRequestFttWelcomeOptions:
        if len(set(self.reference_channel_ids)) != len(self.reference_channel_ids):
            raise ValueError("FTT welcome reference channels must be unique")
        if self.channel_id in self.reference_channel_ids:
            raise ValueError("FTT welcome channel cannot also be a reference channel")
        return self


class RoleRequestFttEntryOptions(FrozenConfigModel):
    base_role_ids: tuple[int, ...] = Field(min_length=1, max_length=9)
    stations: tuple[RoleRequestStationOption, ...] = Field(min_length=1, max_length=10)
    welcome: RoleRequestFttWelcomeOptions | None = None

    @model_validator(mode="after")
    def validate_role_catalog(self) -> RoleRequestFttEntryOptions:
        station_keys = tuple(station.key for station in self.stations)
        station_role_ids = tuple(station.role_id for station in self.stations)
        if len(set(self.base_role_ids)) != len(self.base_role_ids):
            raise ValueError("FTT entry base roles must be unique")
        if len(set(station_keys)) != len(station_keys):
            raise ValueError("FTT entry station keys must be unique")
        if len(set(station_role_ids)) != len(station_role_ids):
            raise ValueError("FTT entry station roles must be unique")
        if set(self.base_role_ids) & set(station_role_ids):
            raise ValueError("FTT entry base and station roles must be distinct")
        return self


class RoleRequestShiftOption(RoleRequestStationOption):
    description: str = Field(min_length=1, max_length=100)


class RoleRequestOptions(FrozenConfigModel):
    retired_role_id: int | None = Field(default=None, gt=0)
    ftt_entry: RoleRequestFttEntryOptions | None = None
    shifts: tuple[RoleRequestShiftOption, ...] = Field(default=(), max_length=25)

    @model_validator(mode="after")
    def validate_shifts(self) -> RoleRequestOptions:
        ids = [shift.role_id for shift in self.shifts]
        if len(set(ids)) != len(ids) or len({shift.key for shift in self.shifts}) != len(ids):
            raise ValueError("Shift keys and roles must be unique")
        other_ids = {self.retired_role_id}
        if self.ftt_entry:
            other_ids.update(self.ftt_entry.base_role_ids)
            other_ids.update(station.role_id for station in self.ftt_entry.stations)
        if other_ids.intersection(ids):
            raise ValueError("Shift roles must be distinct from entry and retirement roles")
        return self


class PagerOptions(FrozenConfigModel):
    starting_number: int = Field(ge=1, le=99_999, strict=True)
    forward_channel_id: int | None = Field(default=None, gt=0, strict=True)


class FactionStatusOptions(FrozenConfigModel):
    faction_id: int = Field(gt=0)


class FleetOptions(FrozenConfigModel):
    faction_id: int = Field(gt=0)
    cache_seconds: int = Field(default=120, ge=1, le=3600, strict=True)


class FtpStatusOptions(FrozenConfigModel):
    faction_id: int = Field(gt=0)
    supervisory_role_ids: tuple[int, ...] = Field(min_length=1, max_length=10)
    mfto_role_ids: tuple[int, ...] = Field(min_length=1, max_length=10)
    fto_role_ids: tuple[int, ...] = Field(min_length=1, max_length=10)
    ftt_role_ids: tuple[int, ...] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def validate_role_catalog(self) -> FtpStatusOptions:
        groups = (
            self.supervisory_role_ids,
            self.mfto_role_ids,
            self.fto_role_ids,
            self.ftt_role_ids,
        )
        if any(any(role_id <= 0 for role_id in group) for group in groups):
            raise ValueError("FTP role IDs must be positive")
        flattened = tuple(role_id for group in groups for role_id in group)
        if len(flattened) != len(set(flattened)):
            raise ValueError("FTP role groups must contain unique role IDs")
        return self

    @property
    def all_role_ids(self) -> tuple[int, ...]:
        return (
            *self.supervisory_role_ids,
            *self.mfto_role_ids,
            *self.fto_role_ids,
            *self.ftt_role_ids,
        )


class FtpRequestOptions(FrozenConfigModel):
    channel_id: int = Field(gt=0)
    additional_command_channel_ids: tuple[int, ...] = Field(default=(), max_length=10)
    command_role_ids: tuple[int, ...] = Field(min_length=1, max_length=10)
    executive_role_ids: tuple[int, ...] = Field(min_length=1, max_length=10)
    supervisor_extra_notification_role_ids: tuple[Annotated[int, Field(gt=0, strict=True)], ...] = Field(
        default=(), max_length=10
    )
    expiry_enabled: bool = True
    expiry_check_minutes: int = Field(default=10, ge=1, le=1440)
    expiry_grace_hours: int = Field(default=6, ge=1, le=168)

    @model_validator(mode="after")
    def validate_management_roles(self) -> FtpRequestOptions:
        role_ids = (*self.command_role_ids, *self.executive_role_ids)
        if any(role_id <= 0 for role_id in role_ids):
            raise ValueError("FTP management role IDs must be positive")
        if len(role_ids) != len(set(role_ids)):
            raise ValueError("FTP management role IDs must be unique")
        extra_roles = self.supervisor_extra_notification_role_ids
        if len(extra_roles) != len(set(extra_roles)):
            raise ValueError("FTP supervisor notification role IDs must be unique")
        channel_ids = (self.channel_id, *self.additional_command_channel_ids)
        if any(channel_id <= 0 for channel_id in channel_ids):
            raise ValueError("FTP request channel IDs must be positive")
        if len(channel_ids) != len(set(channel_ids)):
            raise ValueError("FTP request channel IDs must be unique")
        return self

    @property
    def management_role_ids(self) -> tuple[int, ...]:
        return (*self.command_role_ids, *self.executive_role_ids)

    @property
    def command_channel_ids(self) -> tuple[int, ...]:
        return (self.channel_id, *self.additional_command_channel_ids)


class WarrantNotificationOptions(FrozenConfigModel):
    """Destination and mentions for warrant notifications."""

    channel_id: int = Field(gt=0)
    result_channel_id: int | None = Field(default=None, gt=0)
    command_role_ids: tuple[int, ...] = Field(min_length=1, max_length=10)
    executive_role_ids: tuple[int, ...] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def validate_notification_roles(self) -> WarrantNotificationOptions:
        role_ids = (*self.command_role_ids, *self.executive_role_ids)
        if any(role_id <= 0 for role_id in role_ids):
            raise ValueError("warrant notification role IDs must be positive")
        if len(role_ids) != len(set(role_ids)):
            raise ValueError("warrant notification roles must be unique")
        return self

    @property
    def notification_role_ids(self) -> tuple[int, ...]:
        return (*self.executive_role_ids, *self.command_role_ids)

    @property
    def resolution_channel_id(self) -> int:
        return self.result_channel_id or self.channel_id


class FactionMemberAnalysisOptions(FrozenConfigModel):
    faction_id: int = Field(gt=0)
    base_divisions: tuple[str, ...] = Field(min_length=1, max_length=10)
    inactivity_warning_days: int = Field(default=14, ge=1, le=90)
    inactivity_critical_days: int = Field(default=30, ge=2, le=365)
    inactivity_warning_percentage: int = Field(default=20, ge=1, le=100)
    inactivity_critical_percentage: int = Field(default=30, ge=2, le=100)
    off_duty_alert_min_connected: int = Field(default=5, ge=2, le=500)
    off_duty_alert_percentage: int = Field(default=70, ge=1, le=100)

    @model_validator(mode="after")
    def validate_analysis_thresholds(self) -> FactionMemberAnalysisOptions:
        if self.inactivity_critical_days <= self.inactivity_warning_days:
            raise ValueError("critical inactivity days must exceed warning days")
        if self.inactivity_critical_percentage <= self.inactivity_warning_percentage:
            raise ValueError("critical inactivity percentage must exceed warning percentage")
        normalized = tuple(name.strip().casefold() for name in self.base_divisions)
        if any(not name for name in normalized) or len(set(normalized)) != len(normalized):
            raise ValueError("base divisions must be non-empty and unique")
        return self


class ArrestReportLookupOptions(FrozenConfigModel):
    faction_id: int = Field(gt=0)
    report_attachment_mode: ArrestReportAttachmentMode = ArrestReportAttachmentMode.GENERATED
    max_rank_names_per_lookup: int = Field(default=20, ge=1, le=50, strict=True)
    warrant_alerts_enabled: bool = False
    warrant_alert_channel_ids: tuple[int, ...] = Field(default=(), max_length=10)
    warrant_alert_role_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_warrant_alerts(self) -> ArrestReportLookupOptions:
        if any(channel_id <= 0 for channel_id in self.warrant_alert_channel_ids):
            raise ValueError("warrant alert channels must contain positive IDs")
        if len(set(self.warrant_alert_channel_ids)) != len(self.warrant_alert_channel_ids):
            raise ValueError("warrant alert channels must be unique")
        if self.warrant_alerts_enabled and (
            not self.warrant_alert_channel_ids or self.warrant_alert_role_id is None
        ):
            raise ValueError("enabled warrant alerts require channels and a role")
        return self


class PendingArrestDigestTarget(FrozenConfigModel):
    channel_id: int = Field(gt=0)
    mention_users: bool = False


class PendingArrestReportsOptions(FrozenConfigModel):
    faction_id: int = Field(gt=0)
    max_range_days: int = Field(default=90, ge=1, le=365)
    max_records: int = Field(default=1_000, ge=50, le=5_000)
    digest_interval_hours: int = Field(default=6, ge=1, le=168)
    digest_lookback_days: int = Field(default=7, ge=1, le=90)
    digest_targets: tuple[PendingArrestDigestTarget, ...] = Field(default=(), max_length=10)
    supervisor_alerts_enabled: bool = False
    supervisor_alert_channel_ids: tuple[int, ...] = Field(default=(), max_length=10)
    supervisor_alert_role_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_digest_targets(self) -> PendingArrestReportsOptions:
        channel_ids = [target.channel_id for target in self.digest_targets]
        if len(channel_ids) != len(set(channel_ids)):
            raise ValueError("digest target channels must be unique")
        if sum(target.mention_users for target in self.digest_targets) > 1:
            raise ValueError("only one digest target can mention users")
        if self.digest_lookback_days > self.max_range_days:
            raise ValueError("digest lookback cannot exceed max range")
        if any(channel_id <= 0 for channel_id in self.supervisor_alert_channel_ids):
            raise ValueError("supervisor alert channels must contain positive IDs")
        if len(set(self.supervisor_alert_channel_ids)) != len(self.supervisor_alert_channel_ids):
            raise ValueError("supervisor alert channels must be unique")
        if set(self.supervisor_alert_channel_ids).intersection(channel_ids):
            raise ValueError("supervisor alert channels must differ from digest targets")
        if self.supervisor_alerts_enabled and (
            not self.supervisor_alert_channel_ids or self.supervisor_alert_role_id is None
        ):
            raise ValueError("enabled supervisor alerts require channels and a role")
        if self.supervisor_alerts_enabled and not any(target.mention_users for target in self.digest_targets):
            raise ValueError("enabled supervisor alerts require a mentioning digest target")
        return self


class RumorChannelOptions(FrozenConfigModel):
    channel_id: int = Field(gt=0)
    role_id: int = Field(gt=0)


class RumorOptions(FrozenConfigModel):
    channels: tuple[RumorChannelOptions, ...] = Field(min_length=1, max_length=25)

    @model_validator(mode="after")
    def validate_channels(self) -> RumorOptions:
        channel_ids = [item.channel_id for item in self.channels]
        if len(channel_ids) != len(set(channel_ids)):
            raise ValueError("rumor channels must be unique")
        return self

    def target_for_channel(self, channel_id: int) -> RumorChannelOptions | None:
        return next((item for item in self.channels if item.channel_id == channel_id), None)


class FactionReportOptions(FrozenConfigModel):
    faction_id: int = Field(gt=0)
    max_range_days: int = Field(default=366, ge=1, le=730)
    max_bank_records: int = Field(default=10_000, ge=500, le=25_000)


class ArrestStatisticsOptions(FrozenConfigModel):
    faction_id: int = Field(gt=0)
    max_range_days: int = Field(default=30, ge=1, le=90)
    max_reports: int = Field(default=200, ge=10, le=1_000)
    cache_ttl_seconds: int = Field(default=900, ge=30, le=3_600)
    detail_concurrency: int = Field(default=3, ge=1, le=5)
    max_top: Literal[5, 10] = 10


class RadioAuditOptions(FrozenConfigModel):
    faction_id: int = Field(gt=0)
    query_max_range_days: int = Field(default=30, ge=1, le=90)
    query_max_records: int = Field(default=2_000, ge=100, le=10_000)
    audit_max_range_days: int = Field(default=7, ge=1, le=30)
    audit_max_records: int = Field(default=500, ge=50, le=1_000)

    @model_validator(mode="after")
    def validate_relative_limits(self) -> RadioAuditOptions:
        if self.audit_max_range_days > self.query_max_range_days:
            raise ValueError("audit range cannot exceed deterministic query range")
        if self.audit_max_records > self.query_max_records:
            raise ValueError("audit records cannot exceed deterministic query records")
        return self


class OperationalResourcesOptions(FrozenConfigModel):
    faction_id: int = Field(gt=0)
    game_server_address: str = Field(
        min_length=3,
        max_length=255,
        pattern=r"^[A-Za-z0-9.-]+:[1-9][0-9]{0,4}$",
    )
    pcu_url: HttpUrl
    forum_url: HttpUrl
    manual_url: HttpUrl
    penal_code_url: HttpUrl

    @model_validator(mode="after")
    def validate_game_server_port(self) -> OperationalResourcesOptions:
        port = int(self.game_server_address.rsplit(":", maxsplit=1)[1])
        if port > 65_535:
            raise ValueError("game_server_address port must be at most 65535")
        return self


class ForumShortcutLink(FrozenConfigModel):
    label: str = Field(min_length=1, max_length=80)
    url: HttpUrl


class ForumShortcutSection(FrozenConfigModel):
    title: str = Field(min_length=1, max_length=120)
    links: tuple[ForumShortcutLink, ...] = Field(min_length=1, max_length=10)


class ForumShortcutsOptions(FrozenConfigModel):
    organization_name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=1_000)
    banner_url: HttpUrl
    sections: tuple[ForumShortcutSection, ...] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def validate_catalog(self) -> ForumShortcutsOptions:
        section_titles: set[str] = set()
        link_urls: set[str] = set()
        link_count = 0
        for section in self.sections:
            normalized_title = section.title.casefold().strip()
            if normalized_title in section_titles:
                raise ValueError("forum shortcut section titles must be unique")
            section_titles.add(normalized_title)
            rendered = "\n".join(f"└─ [{link.label}]({link.url})" for link in section.links)
            if len(rendered) > 1_024:
                raise ValueError("forum shortcut section exceeds Discord field limits")
            for link in section.links:
                normalized_url = str(link.url)
                if normalized_url in link_urls:
                    raise ValueError("forum shortcut URLs must be unique")
                link_urls.add(normalized_url)
                link_count += 1
        if link_count > 20:
            raise ValueError("forum shortcuts support at most 20 links")
        return self


class PcuProfileKind(StrEnum):
    CHARACTER = "character"
    ACCOUNT = "account"


class BlacklistTypeOptions(FrozenConfigModel):
    """One administrator-defined blacklist type and its registration channels."""

    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,39}$")
    display_name: str = Field(min_length=1, max_length=50)
    reference_prefix: str = Field(pattern=r"^[A-Z][A-Z0-9-]{1,11}$")
    channel_ids: tuple[int, ...] = Field(min_length=1, max_length=10)
    profile_kind: PcuProfileKind = PcuProfileKind.CHARACTER
    max_profiles: int = Field(default=1, ge=1, le=5)
    subject_label: str = Field(default="Personaje", min_length=1, max_length=30)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_channels(self) -> BlacklistTypeOptions:
        if any(channel_id <= 0 for channel_id in self.channel_ids):
            raise ValueError("blacklist channel IDs must be positive")
        if len(self.channel_ids) != len(set(self.channel_ids)):
            raise ValueError("blacklist channel IDs must be unique within a type")
        if not self.display_name.strip():
            raise ValueError("blacklist display name cannot be blank")
        if not self.subject_label.strip():
            raise ValueError("blacklist subject label cannot be blank")
        return self


class BlacklistOptions(FrozenConfigModel):
    """Versioned registry of blacklist types available in one guild."""

    types: tuple[BlacklistTypeOptions, ...] = Field(default=(), max_length=25)
    manager_role_ids: tuple[int, ...] = Field(default=(), max_length=10)

    @model_validator(mode="after")
    def validate_registry(self) -> BlacklistOptions:
        if any(role_id <= 0 for role_id in self.manager_role_ids):
            raise ValueError("blacklist manager role IDs must be positive")
        if len(self.manager_role_ids) != len(set(self.manager_role_ids)):
            raise ValueError("blacklist manager role IDs must be unique")
        keys: set[str] = set()
        prefixes: set[str] = set()
        channels: set[int] = set()
        for item in self.types:
            if item.key in keys:
                raise ValueError("blacklist type keys must be unique")
            keys.add(item.key)
            if item.reference_prefix in prefixes:
                raise ValueError("blacklist reference prefixes must be unique")
            prefixes.add(item.reference_prefix)
            overlap = channels.intersection(item.channel_ids)
            if overlap:
                raise ValueError("a blacklist channel can belong to only one type")
            channels.update(item.channel_ids)
        return self

    def type_for_channel(self, channel_id: int) -> BlacklistTypeOptions | None:
        return next(
            (item for item in self.types if item.enabled and channel_id in item.channel_ids),
            None,
        )


class GuildConfigSnapshot(FrozenConfigModel):
    guild_id: int = Field(gt=0)
    revision_id: UUID
    revision: int = Field(ge=1)
    agent: GuildAgentConfig
    policies: dict[AccessPolicyKey, AccessPolicySnapshot]
    features: dict[str, GuildFeatureSnapshot]
    persona_revision_id: UUID
    persona_id: str = Field(pattern=r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
    persona_revision: int = Field(ge=1)

    @model_validator(mode="after")
    def require_policies(self) -> GuildConfigSnapshot:
        missing = set(AccessPolicyKey) - set(self.policies)
        if missing:
            raise ValueError(
                "guild configuration is missing policies: "
                + ", ".join(sorted(item.value for item in missing))
            )
        failure_notifications = self.features.get("review_failure_notifications")
        if failure_notifications is not None and failure_notifications.enabled:
            if failure_notifications.schema_version != 1:
                raise ValueError("review_failure_notifications requires feature schema version 1")
            ReviewFailureNotificationOptions.model_validate(failure_notifications.payload)
        role_requests_feature = self.features.get("role_requests")
        if role_requests_feature is not None and role_requests_feature.enabled:
            if role_requests_feature.schema_version != 1:
                raise ValueError("role_requests requires feature schema version 1")
            options = RoleRequestOptions.model_validate(role_requests_feature.payload)
            if options.shifts:
                allowed_roles = self.policies[AccessPolicyKey.ROLE_REQUEST_ALLOWED_ROLES]
                if allowed_roles.mode is not AccessPolicyMode.RESTRICTED or not {
                    shift.role_id for shift in options.shifts
                } <= set(allowed_roles.ids):
                    raise ValueError("Shift roles require an explicit managed-role catalog")
            if options.retired_role_id is not None or options.ftt_entry is not None:
                operational_roles = self.policies[AccessPolicyKey.OPERATIONAL_STAFF_ROLES]
                if operational_roles.mode is not AccessPolicyMode.RESTRICTED or not operational_roles.ids:
                    raise ValueError("guided role requests require restricted operational staff roles")
            if options.retired_role_id is not None:
                allowed_roles = self.policies[AccessPolicyKey.ROLE_REQUEST_ALLOWED_ROLES]
                if allowed_roles.mode is not AccessPolicyMode.RESTRICTED:
                    raise ValueError("role request retirement requires an explicit managed-role catalog")
                if options.retired_role_id not in allowed_roles.ids:
                    raise ValueError("role request retirement role must belong to the managed-role catalog")
            if options.ftt_entry is not None:
                allowed_roles = self.policies[AccessPolicyKey.ROLE_REQUEST_ALLOWED_ROLES]
                if allowed_roles.mode is not AccessPolicyMode.RESTRICTED:
                    raise ValueError("FTT entry requests require an explicit managed-role catalog")
                entry_role_ids = {
                    *options.ftt_entry.base_role_ids,
                    *(station.role_id for station in options.ftt_entry.stations),
                }
                if not entry_role_ids <= set(allowed_roles.ids):
                    raise ValueError("FTT entry roles must belong to the managed-role catalog")
        feature = self.features.get("pagers")
        if feature is not None and feature.enabled:
            if feature.schema_version != 1:
                raise ValueError("pagers requires feature schema version 1")
            PagerOptions.model_validate(feature.payload)
            required = (
                AccessPolicyKey.PAGER_CHANNELS,
                AccessPolicyKey.PAGER_CREATOR_ROLES,
                AccessPolicyKey.PAGER_RECIPIENT_ROLES,
                AccessPolicyKey.PAGER_CLOSER_ROLES,
                AccessPolicyKey.OPERATIONAL_STAFF_ROLES,
            )
            for key in required:
                policy = self.policies[key]
                if policy.mode is not AccessPolicyMode.RESTRICTED:
                    raise ValueError(f"enabled pagers requires restricted policy {key.value}")
            if len(self.policies[AccessPolicyKey.PAGER_RECIPIENT_ROLES].ids) > 25:
                raise ValueError("pagers supports at most 25 recipient roles")
        deployment_feature = self.features.get("deployment_notifications")
        if deployment_feature is not None and deployment_feature.enabled:
            if deployment_feature.schema_version != 1 or deployment_feature.payload:
                raise ValueError(
                    "deployment_notifications requires feature schema version 1 with empty options"
                )
            channels = self.policies[AccessPolicyKey.DEPLOYMENT_NOTIFICATION_CHANNELS]
            if channels.mode is not AccessPolicyMode.RESTRICTED:
                raise ValueError("enabled deployment_notifications requires restricted deployment channels")
            incident_channels = self.policies[AccessPolicyKey.DEPLOYMENT_INCIDENT_NOTIFICATION_CHANNELS]
            if incident_channels.mode is AccessPolicyMode.OPEN:
                raise ValueError("deployment incident notification channels cannot be open")
        owner_feature = self.features.get("report_owner_access")
        if owner_feature is not None and owner_feature.enabled:
            if owner_feature.schema_version != 1:
                raise ValueError("report_owner_access requires feature schema version 1")
            ReportOwnerAccessOptions.model_validate(owner_feature.payload)
            channels = self.policies[AccessPolicyKey.REPORT_CHANNELS]
            if channels.mode is not AccessPolicyMode.RESTRICTED:
                raise ValueError("enabled report_owner_access requires explicit restricted report channels")
        service_calls_feature = self.features.get("service_calls")
        if service_calls_feature is not None and service_calls_feature.enabled:
            if service_calls_feature.schema_version != 1 or service_calls_feature.payload:
                raise ValueError("service_calls requires feature schema version 1 with empty options")
            channels = self.policies[AccessPolicyKey.SERVICE_CALL_CHANNELS]
            if channels.mode is not AccessPolicyMode.RESTRICTED:
                raise ValueError("enabled service_calls requires restricted service-call channels")
        stolen_vehicles_feature = self.features.get("stolen_vehicles")
        if stolen_vehicles_feature is not None and stolen_vehicles_feature.enabled:
            if stolen_vehicles_feature.schema_version != 1 or stolen_vehicles_feature.payload:
                raise ValueError("stolen_vehicles requires feature schema version 1 with empty options")
            channels = self.policies[AccessPolicyKey.STOLEN_VEHICLE_CHANNELS]
            if channels.mode is AccessPolicyMode.OPEN:
                raise ValueError("stolen vehicle notification channels cannot be open")
            roles = self.policies[AccessPolicyKey.OPERATIONAL_STAFF_ROLES]
            if roles.mode is not AccessPolicyMode.RESTRICTED:
                raise ValueError("enabled stolen_vehicles requires restricted operational staff roles")
        faction_status_feature = self.features.get("faction_status")
        if faction_status_feature is not None and faction_status_feature.enabled:
            if faction_status_feature.schema_version != 1:
                raise ValueError("faction_status requires feature schema version 1")
            FactionStatusOptions.model_validate(faction_status_feature.payload)
        fleet_feature = self.features.get("fleet")
        if fleet_feature is not None and fleet_feature.enabled:
            if fleet_feature.schema_version != 1:
                raise ValueError("fleet requires feature schema version 1")
            FleetOptions.model_validate(fleet_feature.payload)
            roles = self.policies[AccessPolicyKey.OPERATIONAL_STAFF_ROLES]
            if roles.mode is not AccessPolicyMode.RESTRICTED:
                raise ValueError("enabled fleet requires restricted operational staff roles")
        ftp_status_feature = self.features.get("ftp_status")
        if ftp_status_feature is not None and ftp_status_feature.enabled:
            if ftp_status_feature.schema_version != 1:
                raise ValueError("ftp_status requires feature schema version 1")
            FtpStatusOptions.model_validate(ftp_status_feature.payload)
            roles = self.policies[AccessPolicyKey.OPERATIONAL_STAFF_ROLES]
            if roles.mode is not AccessPolicyMode.RESTRICTED:
                raise ValueError("enabled ftp_status requires restricted operational staff roles")
        ftp_requests_feature = self.features.get("ftp_requests")
        if ftp_requests_feature is not None and ftp_requests_feature.enabled:
            if ftp_requests_feature.schema_version != 1:
                raise ValueError("ftp_requests requires feature schema version 1")
            FtpRequestOptions.model_validate(ftp_requests_feature.payload)
            if ftp_status_feature is None or not ftp_status_feature.enabled:
                raise ValueError("enabled ftp_requests requires enabled ftp_status")
        warrant_notifications_feature = self.features.get("warrant_notifications")
        if warrant_notifications_feature is not None and warrant_notifications_feature.enabled:
            if warrant_notifications_feature.schema_version != 1:
                raise ValueError("warrant_notifications requires feature schema version 1")
            WarrantNotificationOptions.model_validate(warrant_notifications_feature.payload)
            roles = self.policies[AccessPolicyKey.OPERATIONAL_STAFF_ROLES]
            if roles.mode is not AccessPolicyMode.RESTRICTED:
                raise ValueError("enabled warrant_notifications requires restricted operational staff roles")
        member_analysis_feature = self.features.get("faction_member_analysis")
        if member_analysis_feature is not None and member_analysis_feature.enabled:
            if member_analysis_feature.schema_version != 1:
                raise ValueError("faction_member_analysis requires feature schema version 1")
            FactionMemberAnalysisOptions.model_validate(member_analysis_feature.payload)
            roles = self.policies[AccessPolicyKey.SUPERVISOR_ROLES]
            if roles.mode is not AccessPolicyMode.RESTRICTED:
                raise ValueError("enabled faction_member_analysis requires restricted supervisor roles")
        resources_feature = self.features.get("operational_resources")
        if resources_feature is not None and resources_feature.enabled:
            if resources_feature.schema_version != 1:
                raise ValueError("operational_resources requires feature schema version 1")
            OperationalResourcesOptions.model_validate(resources_feature.payload)
            roles = self.policies[AccessPolicyKey.OPERATIONAL_STAFF_ROLES]
            if roles.mode is not AccessPolicyMode.RESTRICTED:
                raise ValueError("enabled operational_resources requires restricted operational staff roles")
        forum_feature = self.features.get("forum_shortcuts")
        if forum_feature is not None and forum_feature.enabled:
            if forum_feature.schema_version != 1:
                raise ValueError("forum_shortcuts requires feature schema version 1")
            ForumShortcutsOptions.model_validate(forum_feature.payload)
        arrest_lookup_feature = self.features.get("arrest_report_lookup")
        if arrest_lookup_feature is not None and arrest_lookup_feature.enabled:
            if arrest_lookup_feature.schema_version != 1:
                raise ValueError("arrest_report_lookup requires feature schema version 1")
            ArrestReportLookupOptions.model_validate(arrest_lookup_feature.payload)
        pending_reports_feature = self.features.get("pending_arrest_reports")
        if pending_reports_feature is not None and pending_reports_feature.enabled:
            if pending_reports_feature.schema_version != 1:
                raise ValueError("pending_arrest_reports requires feature schema version 1")
            PendingArrestReportsOptions.model_validate(pending_reports_feature.payload)
        rumors_feature = self.features.get("rumors")
        if rumors_feature is not None and rumors_feature.enabled:
            if rumors_feature.schema_version != 1:
                raise ValueError("rumors requires feature schema version 1")
            RumorOptions.model_validate(rumors_feature.payload)
        faction_report_feature = self.features.get("faction_reports")
        if faction_report_feature is not None and faction_report_feature.enabled:
            if faction_report_feature.schema_version != 1:
                raise ValueError("faction_reports requires feature schema version 1")
            FactionReportOptions.model_validate(faction_report_feature.payload)
            roles = self.policies[AccessPolicyKey.FACTION_REPORT_ROLES]
            if roles.mode is not AccessPolicyMode.RESTRICTED:
                raise ValueError("enabled faction_reports requires restricted report roles")
        arrest_statistics_feature = self.features.get("arrest_statistics")
        if arrest_statistics_feature is not None and arrest_statistics_feature.enabled:
            if arrest_statistics_feature.schema_version != 1:
                raise ValueError("arrest_statistics requires feature schema version 1")
            ArrestStatisticsOptions.model_validate(arrest_statistics_feature.payload)
            roles = self.policies[AccessPolicyKey.FACTION_REPORT_ROLES]
            if roles.mode is not AccessPolicyMode.RESTRICTED:
                raise ValueError("enabled arrest_statistics requires restricted faction report roles")
            if arrest_lookup_feature is None or not arrest_lookup_feature.enabled:
                raise ValueError("enabled arrest_statistics requires enabled arrest_report_lookup")
        logbook_feature = self.features.get("logbook")
        if logbook_feature is not None and logbook_feature.enabled:
            if logbook_feature.schema_version != 1 or logbook_feature.payload:
                raise ValueError("logbook requires feature schema version 1 with empty options")
            channels = self.policies[AccessPolicyKey.LOGBOOK_CHANNELS]
            if channels.mode is not AccessPolicyMode.RESTRICTED:
                raise ValueError("enabled logbook requires restricted logbook channels")
        blacklist_feature = self.features.get("blacklists")
        if blacklist_feature is not None and blacklist_feature.enabled:
            if blacklist_feature.schema_version != 1:
                raise ValueError("blacklists requires feature schema version 1")
            blacklist_options = BlacklistOptions.model_validate(blacklist_feature.payload)
            if not any(item.enabled for item in blacklist_options.types):
                raise ValueError("enabled blacklists requires at least one enabled type")
            if not blacklist_options.manager_role_ids:
                raise ValueError("enabled blacklists requires manager role IDs")
            operational_roles = self.policies[AccessPolicyKey.OPERATIONAL_STAFF_ROLES]
            if operational_roles.mode is not AccessPolicyMode.RESTRICTED:
                raise ValueError("enabled blacklists requires restricted operational staff roles")
        radio_audit_feature = self.features.get("radio_audit")
        if radio_audit_feature is not None and radio_audit_feature.enabled:
            if radio_audit_feature.schema_version != 1:
                raise ValueError("radio_audit requires feature schema version 1")
            RadioAuditOptions.model_validate(radio_audit_feature.payload)
            operational_roles = self.policies[AccessPolicyKey.OPERATIONAL_STAFF_ROLES]
            if operational_roles.mode is not AccessPolicyMode.RESTRICTED:
                raise ValueError("enabled radio_audit requires restricted operational staff roles")
            audit_roles = self.policies[AccessPolicyKey.RADIO_AUDIT_ROLES]
            if audit_roles.mode is not AccessPolicyMode.RESTRICTED:
                raise ValueError("enabled radio_audit requires restricted audit roles")
        return self

    def policy(self, key: AccessPolicyKey) -> AccessPolicySnapshot:
        return self.policies[key]


class GuildRuntimeSettings:
    """Settings-shaped view used by existing deterministic adapters."""

    def __init__(self, process: AppSettings, snapshot: GuildConfigSnapshot) -> None:
        self.process = process
        self.snapshot = snapshot

    def __getattr__(self, name: str) -> Any:
        if hasattr(self.snapshot.agent, name):
            return getattr(self.snapshot.agent, name)
        return getattr(self.process, name)

    @property
    def guild_allowlist(self) -> SnowflakeAllowlist:
        return SnowflakeAllowlist(ids=frozenset({self.snapshot.guild_id}))

    @property
    def report_channel_allowlist(self) -> SnowflakeAllowlist:
        return self.snapshot.policy(AccessPolicyKey.REPORT_CHANNELS).as_allowlist()

    @property
    def user_allowlist(self) -> SnowflakeAllowlist:
        return self.snapshot.policy(AccessPolicyKey.AGENT_USERS).as_allowlist()

    @property
    def agent_role_allowlist(self) -> SnowflakeAllowlist:
        return self.snapshot.policy(AccessPolicyKey.AGENT_ROLES).as_allowlist()

    @property
    def agent_blocked_user_allowlist(self) -> SnowflakeAllowlist:
        return self.snapshot.policy(AccessPolicyKey.AGENT_BLOCKED_USERS).as_allowlist()

    @property
    def supervisor_role_allowlist(self) -> SnowflakeAllowlist:
        return self.snapshot.policy(AccessPolicyKey.SUPERVISOR_ROLES).as_allowlist()

    @property
    def supervisor_role_ids(self) -> tuple[int, ...]:
        policy = self.snapshot.policy(AccessPolicyKey.SUPERVISOR_ROLES)
        return policy.ids if policy.mode is AccessPolicyMode.RESTRICTED else ()

    @property
    def report_analytics_role_allowlist(self) -> SnowflakeAllowlist:
        return self.snapshot.policy(AccessPolicyKey.REPORT_ANALYTICS_ROLES).as_allowlist()

    @property
    def review_escalation_role_ids(self) -> tuple[int, ...]:
        policy = self.snapshot.policy(AccessPolicyKey.REVIEW_ESCALATION_ROLES)
        return policy.ids if policy.mode is AccessPolicyMode.RESTRICTED else ()

    @property
    def role_request_allowed_channel_allowlist(self) -> SnowflakeAllowlist:
        return self.snapshot.policy(AccessPolicyKey.ROLE_REQUEST_CHANNELS).as_allowlist()

    @property
    def role_request_review_channel_ids(self) -> tuple[int, ...]:
        policy = self.snapshot.policy(AccessPolicyKey.ROLE_REQUEST_REVIEW_CHANNELS)
        return policy.ids if policy.mode is AccessPolicyMode.RESTRICTED else ()

    @property
    def role_request_allowed_role_allowlist(self) -> SnowflakeAllowlist:
        return self.snapshot.policy(AccessPolicyKey.ROLE_REQUEST_ALLOWED_ROLES).as_allowlist()

    @property
    def role_request_allowed_role_ids(self) -> tuple[int, ...]:
        policy = self.snapshot.policy(AccessPolicyKey.ROLE_REQUEST_ALLOWED_ROLES)
        return policy.ids if policy.mode is AccessPolicyMode.RESTRICTED else ()

    @property
    def role_request_reviewer_role_ids(self) -> tuple[int, ...]:
        policy = self.snapshot.policy(AccessPolicyKey.ROLE_REQUEST_REVIEWER_ROLES)
        return policy.ids if policy.mode is AccessPolicyMode.RESTRICTED else ()

    @property
    def role_request_retired_role_id(self) -> int | None:
        feature = self.snapshot.features.get("role_requests")
        if feature is None or not feature.enabled:
            return None
        return RoleRequestOptions.model_validate(feature.payload).retired_role_id

    @property
    def role_request_ftt_entry(self) -> RoleRequestFttEntryOptions | None:
        feature = self.snapshot.features.get("role_requests")
        if feature is None or not feature.enabled:
            return None
        return RoleRequestOptions.model_validate(feature.payload).ftt_entry

    @property
    def role_request_shifts(self) -> tuple[RoleRequestShiftOption, ...]:
        feature = self.snapshot.features.get("role_requests")
        if feature is None or not feature.enabled:
            return ()
        return RoleRequestOptions.model_validate(feature.payload).shifts

    @property
    def pager_channel_allowlist(self) -> SnowflakeAllowlist:
        return self.snapshot.policy(AccessPolicyKey.PAGER_CHANNELS).as_allowlist()

    @property
    def pager_creator_role_ids(self) -> tuple[int, ...]:
        return self.snapshot.policy(AccessPolicyKey.PAGER_CREATOR_ROLES).ids

    @property
    def pager_recipient_role_ids(self) -> tuple[int, ...]:
        return self.snapshot.policy(AccessPolicyKey.PAGER_RECIPIENT_ROLES).ids

    @property
    def pager_closer_role_ids(self) -> tuple[int, ...]:
        return self.snapshot.policy(AccessPolicyKey.PAGER_CLOSER_ROLES).ids

    @property
    def operational_staff_role_ids(self) -> tuple[int, ...]:
        policy = self.snapshot.policy(AccessPolicyKey.OPERATIONAL_STAFF_ROLES)
        return policy.ids if policy.mode is AccessPolicyMode.RESTRICTED else ()

    @property
    def radio_audit_role_ids(self) -> tuple[int, ...]:
        policy = self.snapshot.policy(AccessPolicyKey.RADIO_AUDIT_ROLES)
        return policy.ids if policy.mode is AccessPolicyMode.RESTRICTED else ()

    @property
    def faction_report_role_ids(self) -> tuple[int, ...]:
        policy = self.snapshot.policy(AccessPolicyKey.FACTION_REPORT_ROLES)
        return policy.ids if policy.mode is AccessPolicyMode.RESTRICTED else ()

    @property
    def deployment_notification_channel_ids(self) -> tuple[int, ...]:
        policy = self.snapshot.policy(AccessPolicyKey.DEPLOYMENT_NOTIFICATION_CHANNELS)
        return policy.ids if policy.mode is AccessPolicyMode.RESTRICTED else ()

    @property
    def deployment_incident_notification_channel_ids(self) -> tuple[int, ...]:
        policy = self.snapshot.policy(AccessPolicyKey.DEPLOYMENT_INCIDENT_NOTIFICATION_CHANNELS)
        return policy.ids if policy.mode is AccessPolicyMode.RESTRICTED else ()

    @property
    def service_call_channel_ids(self) -> tuple[int, ...]:
        policy = self.snapshot.policy(AccessPolicyKey.SERVICE_CALL_CHANNELS)
        return policy.ids if policy.mode is AccessPolicyMode.RESTRICTED else ()

    @property
    def stolen_vehicle_channel_ids(self) -> tuple[int, ...]:
        policy = self.snapshot.policy(AccessPolicyKey.STOLEN_VEHICLE_CHANNELS)
        return policy.ids if policy.mode is AccessPolicyMode.RESTRICTED else ()

    @property
    def stolen_vehicles_enabled(self) -> bool:
        feature = self.snapshot.features.get("stolen_vehicles")
        return bool(feature is not None and feature.enabled)

    @property
    def logbook_channel_ids(self) -> tuple[int, ...]:
        policy = self.snapshot.policy(AccessPolicyKey.LOGBOOK_CHANNELS)
        return policy.ids if policy.mode is AccessPolicyMode.RESTRICTED else ()

    @property
    def blacklist_options(self) -> BlacklistOptions | None:
        feature = self.snapshot.features.get("blacklists")
        if feature is None or not feature.enabled:
            return None
        return BlacklistOptions.model_validate(feature.payload)

    @property
    def faction_status_faction_id(self) -> int | None:
        feature = self.snapshot.features.get("faction_status")
        if feature is None or not feature.enabled:
            return None
        return FactionStatusOptions.model_validate(feature.payload).faction_id

    @property
    def fleet_options(self) -> FleetOptions | None:
        feature = self.snapshot.features.get("fleet")
        if feature is None or not feature.enabled:
            return None
        return FleetOptions.model_validate(feature.payload)

    @property
    def ftp_status_options(self) -> FtpStatusOptions | None:
        feature = self.snapshot.features.get("ftp_status")
        if feature is None or not feature.enabled:
            return None
        return FtpStatusOptions.model_validate(feature.payload)

    @property
    def ftp_request_options(self) -> FtpRequestOptions | None:
        feature = self.snapshot.features.get("ftp_requests")
        if feature is None or not feature.enabled:
            return None
        return FtpRequestOptions.model_validate(feature.payload)

    @property
    def warrant_notification_options(self) -> WarrantNotificationOptions | None:
        feature = self.snapshot.features.get("warrant_notifications")
        if feature is None or not feature.enabled:
            return None
        return WarrantNotificationOptions.model_validate(feature.payload)

    @property
    def faction_member_analysis_options(self) -> FactionMemberAnalysisOptions | None:
        feature = self.snapshot.features.get("faction_member_analysis")
        if feature is None or not feature.enabled:
            return None
        return FactionMemberAnalysisOptions.model_validate(feature.payload)

    @property
    def operational_resources_options(self) -> OperationalResourcesOptions | None:
        feature = self.snapshot.features.get("operational_resources")
        if feature is None or not feature.enabled:
            return None
        return OperationalResourcesOptions.model_validate(feature.payload)

    @property
    def review_failure_notification_options(self) -> ReviewFailureNotificationOptions | None:
        feature = self.snapshot.features.get("review_failure_notifications")
        if feature is None or not feature.enabled:
            return None
        return ReviewFailureNotificationOptions.model_validate(feature.payload)

    @property
    def forum_shortcuts_options(self) -> ForumShortcutsOptions | None:
        feature = self.snapshot.features.get("forum_shortcuts")
        if feature is None or not feature.enabled:
            return None
        return ForumShortcutsOptions.model_validate(feature.payload)

    @property
    def arrest_report_lookup_faction_id(self) -> int | None:
        feature = self.snapshot.features.get("arrest_report_lookup")
        if feature is None or not feature.enabled:
            return None
        return ArrestReportLookupOptions.model_validate(feature.payload).faction_id

    @property
    def arrest_report_lookup_options(self) -> ArrestReportLookupOptions | None:
        feature = self.snapshot.features.get("arrest_report_lookup")
        if feature is None or not feature.enabled:
            return None
        return ArrestReportLookupOptions.model_validate(feature.payload)

    @property
    def arrest_report_attachment_mode(self) -> ArrestReportAttachmentMode:
        feature = self.snapshot.features.get("arrest_report_lookup")
        if feature is None or not feature.enabled:
            return ArrestReportAttachmentMode.GENERATED
        return ArrestReportLookupOptions.model_validate(feature.payload).report_attachment_mode

    @property
    def max_rank_names_per_lookup(self) -> int:
        feature = self.snapshot.features.get("arrest_report_lookup")
        if feature is None or not feature.enabled:
            return 20
        return ArrestReportLookupOptions.model_validate(feature.payload).max_rank_names_per_lookup

    @property
    def pending_arrest_reports_options(self) -> PendingArrestReportsOptions | None:
        feature = self.snapshot.features.get("pending_arrest_reports")
        if feature is None or not feature.enabled:
            return None
        return PendingArrestReportsOptions.model_validate(feature.payload)

    @property
    def rumor_options(self) -> RumorOptions | None:
        feature = self.snapshot.features.get("rumors")
        if feature is None or not feature.enabled:
            return None
        return RumorOptions.model_validate(feature.payload)

    @property
    def pending_arrest_reports_faction_id(self) -> int | None:
        options = self.pending_arrest_reports_options
        return options.faction_id if options is not None else None

    @property
    def pending_arrest_reports_max_range_days(self) -> int:
        options = self.pending_arrest_reports_options
        return options.max_range_days if options is not None else 90

    @property
    def pending_arrest_reports_max_records(self) -> int:
        options = self.pending_arrest_reports_options
        return options.max_records if options is not None else 1_000

    @property
    def faction_report_options(self) -> FactionReportOptions | None:
        feature = self.snapshot.features.get("faction_reports")
        if feature is None or not feature.enabled:
            return None
        return FactionReportOptions.model_validate(feature.payload)

    @property
    def arrest_statistics_options(self) -> ArrestStatisticsOptions | None:
        feature = self.snapshot.features.get("arrest_statistics")
        if feature is None or not feature.enabled:
            return None
        return ArrestStatisticsOptions.model_validate(feature.payload)

    @property
    def radio_audit_options(self) -> RadioAuditOptions | None:
        feature = self.snapshot.features.get("radio_audit")
        if feature is None or not feature.enabled:
            return None
        return RadioAuditOptions.model_validate(feature.payload)

    @property
    def pager_starting_number(self) -> int:
        feature = self.snapshot.features.get("pagers")
        if feature is None:
            return 1
        return PagerOptions.model_validate(feature.payload).starting_number

    @property
    def pager_forward_channel_id(self) -> int | None:
        feature = self.snapshot.features.get("pagers")
        if feature is None or not feature.enabled:
            return None
        return PagerOptions.model_validate(feature.payload).forward_channel_id

    @property
    def report_owner_access_max_rounds(self) -> int | None:
        feature = self.snapshot.features.get("report_owner_access")
        if feature is None or not feature.enabled:
            return None
        return ReportOwnerAccessOptions.model_validate(feature.payload).max_rounds

    @property
    def report_owner_quota_grant_role_ids(self) -> tuple[int, ...]:
        feature = self.snapshot.features.get("report_owner_access")
        if feature is None or not feature.enabled:
            return ()
        return ReportOwnerAccessOptions.model_validate(feature.payload).quota_grant_role_ids

    @property
    def report_owner_quota_exhausted_notification_channel_id(self) -> int | None:
        feature = self.snapshot.features.get("report_owner_access")
        if feature is None or not feature.enabled:
            return None
        return ReportOwnerAccessOptions.model_validate(
            feature.payload
        ).quota_exhausted_notification_channel_id

    @property
    def openai_model(self) -> str:
        return self.snapshot.agent.model

    @property
    def openai_reasoning_effort(self) -> str:
        return self.snapshot.agent.reasoning_effort

    @property
    def max_images_per_review(self) -> int:
        return min(self.snapshot.agent.max_images_per_review, self.process.max_images_per_review)

    @property
    def max_image_bytes(self) -> int:
        return min(self.snapshot.agent.max_image_bytes, self.process.max_image_bytes)

    @property
    def max_files_per_review(self) -> int:
        return min(self.snapshot.agent.max_files_per_review, self.process.max_files_per_review)

    @property
    def max_file_bytes(self) -> int:
        return min(self.snapshot.agent.max_file_bytes, self.process.max_file_bytes)

    @property
    def max_total_file_bytes(self) -> int:
        return min(self.snapshot.agent.max_total_file_bytes, self.process.max_total_file_bytes)

    @property
    def teamspeak_address(self) -> str:
        feature = self.snapshot.features.get("teamspeak")
        return str((feature.payload if feature else {}).get("address", "172.96.11.180:11189"))
