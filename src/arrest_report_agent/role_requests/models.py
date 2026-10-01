"""Strict state reconstructed from one Discord role-request message."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_MANUAL_ROLES_PER_OPERATION = 10
MAX_ROLES_PER_OPERATION = 40
Snowflake = Annotated[int, Field(gt=0)]


class RoleRequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    PARTIALLY_APPROVED = "partially_approved"
    REJECTED = "rejected"
    FAILED = "failed"


class RoleRequestKind(StrEnum):
    NORMAL = "normal"
    RETIREMENT = "retirement"
    FTT_ENTRY = "ftt_entry"


class StoredRoleRequestStatus(StrEnum):
    """Lifecycle persisted for private, mirrored role-request reviews."""

    PUBLISHING = "publishing"
    PENDING = "pending"
    PROCESSING = "processing"
    APPROVED = "approved"
    PARTIALLY_APPROVED = "partially_approved"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            StoredRoleRequestStatus.APPROVED,
            StoredRoleRequestStatus.PARTIALLY_APPROVED,
            StoredRoleRequestStatus.REJECTED,
            StoredRoleRequestStatus.FAILED,
            StoredRoleRequestStatus.CANCELLED,
        }


class RoleRequestShiftChange(BaseModel):
    """Snapshot of a mutually exclusive shift change, approved as one unit."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    target_role_id: Snowflake
    previous_role_ids: tuple[Snowflake, ...] = Field(default=(), max_length=25)

    @model_validator(mode="after")
    def validate_previous(self) -> RoleRequestShiftChange:
        if self.target_role_id in self.previous_role_ids or len(set(self.previous_role_ids)) != len(
            self.previous_role_ids
        ):
            raise ValueError("Previous shifts must be unique and exclude the target")
        return self

    @property
    def role_ids(self) -> frozenset[int]:
        return frozenset((self.target_role_id, *self.previous_role_ids))


class StoredRoleRequest(BaseModel):
    """Database-backed request used by the v2 public/private flow."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: UUID
    guild_id: Snowflake
    interaction_id: Snowflake
    requester_id: Snowflake
    kind: RoleRequestKind
    shift_change: RoleRequestShiftChange | None = None
    add_role_ids: tuple[Snowflake, ...] = Field(max_length=MAX_ROLES_PER_OPERATION)
    remove_role_ids: tuple[Snowflake, ...] = Field(max_length=MAX_ROLES_PER_OPERATION)
    reason: str | None = Field(default=None, max_length=1000)
    status: StoredRoleRequestStatus
    public_channel_id: Snowflake
    public_message_id: Snowflake | None = None
    reviewer_id: Snowflake | None = None
    decision_comment: str | None = Field(default=None, max_length=1000)
    approved_add_role_ids: tuple[Snowflake, ...] = Field(
        default=(), max_length=MAX_ROLES_PER_OPERATION
    )
    approved_remove_role_ids: tuple[Snowflake, ...] = Field(
        default=(), max_length=MAX_ROLES_PER_OPERATION
    )
    unapproved_add_role_ids: tuple[Snowflake, ...] = Field(
        default=(), max_length=MAX_ROLES_PER_OPERATION
    )
    unapproved_remove_role_ids: tuple[Snowflake, ...] = Field(
        default=(), max_length=MAX_ROLES_PER_OPERATION
    )
    requester_dm_delivered: bool | None = None
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    decided_at: datetime | None = None
    replaced_by_request_id: UUID | None = None
    cancellation_sync_due: datetime | None = None

    @model_validator(mode="after")
    def validate_guided_entry(self) -> StoredRoleRequest:
        if self.kind is RoleRequestKind.FTT_ENTRY and (
            set(self.remove_role_ids) - set(self.shift_change.previous_role_ids if self.shift_change else ())
            or not self.reason
        ):
            raise ValueError("an FTT entry request requires a station and cannot remove roles")
        return self

    def as_legacy_request(self, *, source_channel_id: int, source_message_id: int) -> RoleRequest:
        """Adapt persisted state to the validated role-change engine."""

        return RoleRequest(
            message_id=source_message_id,
            guild_id=self.guild_id,
            channel_id=source_channel_id,
            requester_id=self.requester_id,
            kind=self.kind,
            shift_change=self.shift_change,
            add_role_ids=self.add_role_ids,
            remove_role_ids=self.remove_role_ids,
            reason=self.reason,
            created_at=self.created_at,
        )


class RoleRequestReviewMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    channel_id: Snowflake
    message_id: Snowflake
    created_at: datetime
    updated_at: datetime


class RoleRequest(BaseModel):
    """One pending request recovered exclusively from a Discord message."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    message_id: int = Field(gt=0)
    guild_id: int = Field(gt=0)
    channel_id: int = Field(gt=0)
    requester_id: int = Field(gt=0)
    kind: RoleRequestKind = RoleRequestKind.NORMAL
    shift_change: RoleRequestShiftChange | None = None
    add_role_ids: tuple[Snowflake, ...] = Field(max_length=MAX_ROLES_PER_OPERATION)
    remove_role_ids: tuple[Snowflake, ...] = Field(max_length=MAX_ROLES_PER_OPERATION)
    reason: str | None = Field(default=None, max_length=1000)
    created_at: datetime

    @model_validator(mode="after")
    def validate_role_sets(self) -> RoleRequest:
        if not self.add_role_ids and not self.remove_role_ids:
            raise ValueError("role request must contain at least one role change")
        if self.kind is RoleRequestKind.RETIREMENT and len(self.add_role_ids) != 1:
            raise ValueError("a retirement request must add exactly one role")
        if self.kind is RoleRequestKind.FTT_ENTRY and set(self.remove_role_ids) - set(
            self.shift_change.previous_role_ids if self.shift_change else ()
        ):
            raise ValueError("an FTT entry request cannot remove roles")
        if self.shift_change and (
            self.kind is RoleRequestKind.RETIREMENT
            or not set(self.shift_change.previous_role_ids) <= set(self.remove_role_ids)
            or self.shift_change.target_role_id in self.remove_role_ids
            or set(self.shift_change.previous_role_ids).intersection(self.add_role_ids)
        ):
            raise ValueError("Shift change does not match the requested operations")
        if self.kind is RoleRequestKind.FTT_ENTRY and not self.reason:
            raise ValueError("an FTT entry request must identify its station")
        if len(set(self.add_role_ids)) != len(self.add_role_ids):
            raise ValueError("roles to add must be unique")
        if len(set(self.remove_role_ids)) != len(self.remove_role_ids):
            raise ValueError("roles to remove must be unique")
        if set(self.add_role_ids) & set(self.remove_role_ids):
            raise ValueError("a role cannot be added and removed in the same request")
        return self


class RoleRequestDecision(BaseModel):
    """Terminal decision used to render the immutable audit result."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    status: RoleRequestStatus
    reviewer_id: int = Field(gt=0)
    approved_add_role_ids: tuple[Snowflake, ...] = Field(
        default=(),
        max_length=MAX_ROLES_PER_OPERATION,
    )
    approved_remove_role_ids: tuple[Snowflake, ...] = Field(
        default=(),
        max_length=MAX_ROLES_PER_OPERATION,
    )
    unapproved_add_role_ids: tuple[Snowflake, ...] = Field(
        default=(),
        max_length=MAX_ROLES_PER_OPERATION,
    )
    unapproved_remove_role_ids: tuple[Snowflake, ...] = Field(
        default=(),
        max_length=MAX_ROLES_PER_OPERATION,
    )
    comment: str | None = Field(default=None, max_length=1000)
    decided_at: datetime

    @model_validator(mode="after")
    def validate_status_payload(self) -> RoleRequestDecision:
        if self.status is RoleRequestStatus.PENDING:
            raise ValueError("a decision cannot remain pending")
        role_groups = (
            self.approved_add_role_ids,
            self.approved_remove_role_ids,
            self.unapproved_add_role_ids,
            self.unapproved_remove_role_ids,
        )
        flattened = tuple(role_id for group in role_groups for role_id in group)
        if len(flattened) != len(set(flattened)):
            raise ValueError("a decided role change must appear exactly once")
        approved = self.approved_add_role_ids or self.approved_remove_role_ids
        unapproved = self.unapproved_add_role_ids or self.unapproved_remove_role_ids
        if self.status is RoleRequestStatus.APPROVED and (not approved or unapproved):
            raise ValueError("an approval must approve every requested change")
        if self.status is RoleRequestStatus.PARTIALLY_APPROVED and (not approved or not unapproved):
            raise ValueError("a partial approval requires approved and unapproved changes")
        if self.status in {RoleRequestStatus.REJECTED, RoleRequestStatus.FAILED} and (
            approved or not unapproved
        ):
            raise ValueError("a rejection or failure must leave every change unapproved")
        return self
