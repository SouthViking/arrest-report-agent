"""Strict state reconstructed from one Discord role-request message."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_ROLES_PER_OPERATION = 10
Snowflake = Annotated[int, Field(gt=0)]


class RoleRequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    PARTIALLY_APPROVED = "partially_approved"
    REJECTED = "rejected"
    FAILED = "failed"


class RoleRequest(BaseModel):
    """One pending request recovered exclusively from a Discord message."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    message_id: int = Field(gt=0)
    guild_id: int = Field(gt=0)
    channel_id: int = Field(gt=0)
    requester_id: int = Field(gt=0)
    add_role_ids: tuple[Snowflake, ...] = Field(max_length=MAX_ROLES_PER_OPERATION)
    remove_role_ids: tuple[Snowflake, ...] = Field(max_length=MAX_ROLES_PER_OPERATION)
    reason: str | None = Field(default=None, max_length=1000)
    created_at: datetime

    @model_validator(mode="after")
    def validate_role_sets(self) -> RoleRequest:
        if not self.add_role_ids and not self.remove_role_ids:
            raise ValueError("role request must contain at least one role change")
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
        if self.status is RoleRequestStatus.PARTIALLY_APPROVED and (
            not approved or not unapproved
        ):
            raise ValueError("a partial approval requires approved and unapproved changes")
        if self.status in {RoleRequestStatus.REJECTED, RoleRequestStatus.FAILED} and (
            approved or not unapproved
        ):
            raise ValueError("a rejection or failure must leave every change unapproved")
        return self
