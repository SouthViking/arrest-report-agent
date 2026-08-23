"""Typed application models for faction radio logs and audit results."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RadioLogScanTooLargeError(RuntimeError):
    """Raised before downloading a result set that exceeds a configured bound."""

    def __init__(self, *, total: int, limit: int) -> None:
        super().__init__(f"radio log scan contains {total} rows; maximum is {limit}")
        self.total = total
        self.limit = limit


class ParsedRadioLog(BaseModel):
    """Normalized SARP radio record with deterministic prefix extraction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int = Field(gt=0)
    faction_id: int = Field(gt=0)
    character_id: int = Field(gt=0)
    character_name: str = Field(min_length=1, max_length=100)
    created_at: datetime
    raw_message: str = Field(min_length=1, max_length=4_000)
    channel: str | None = Field(default=None, max_length=100)
    slot: int | None = Field(default=None, gt=0)
    speaker: str | None = Field(default=None, max_length=100)
    body: str = Field(min_length=1, max_length=4_000)
    malformed: bool = False


class RadioLogQueryResult(BaseModel):
    """Complete bounded query result shared by Discord and the audit runner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    faction_id: int = Field(gt=0)
    date_from: str
    date_to: str
    slot: int | None = Field(default=None, gt=0)
    member_query: str | None = None
    raw_total: int = Field(ge=0)
    scanned_count: int = Field(ge=0)
    malformed_count: int = Field(ge=0)
    items: tuple[ParsedRadioLog, ...] = ()


class RadioAuditSeverity(StrEnum):
    LIGHT = "leve"
    MODERATE = "moderada"
    SEVERE = "grave"


class RadioAuditFindingInput(BaseModel):
    """Structured finding proposed by the model through the terminal tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    category: str = Field(min_length=1, max_length=80)
    severity: RadioAuditSeverity
    explanation: str = Field(min_length=1, max_length=700)
    manual_reference: str = Field(min_length=1, max_length=160)
    radio_ids: tuple[int, ...] = Field(min_length=1, max_length=12)

    @field_validator("radio_ids")
    @classmethod
    def unique_radio_ids(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if any(value <= 0 for value in values):
            raise ValueError("radio IDs must be positive")
        return tuple(dict.fromkeys(values))


class RadioAuditFinding(BaseModel):
    """Validated finding attached to one real faction member."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    member_name: str = Field(min_length=1, max_length=100)
    category: str = Field(min_length=1, max_length=80)
    severity: RadioAuditSeverity
    explanation: str = Field(min_length=1, max_length=700)
    manual_reference: str = Field(min_length=1, max_length=160)
    radio_ids: tuple[int, ...] = Field(min_length=1, max_length=12)


class RadioAuditMemberResult(BaseModel):
    """All findings for one member plus the backend-computed overall severity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    member_name: str = Field(min_length=1, max_length=100)
    overall_severity: RadioAuditSeverity
    findings: tuple[RadioAuditFinding, ...] = Field(min_length=1)


class RadioAuditResult(BaseModel):
    """Final sanitized audit projection rendered by Discord."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(min_length=1, max_length=1_000)
    total_radios: int = Field(ge=0)
    total_members: int = Field(ge=0)
    clean_members: int = Field(ge=0)
    light_count: int = Field(ge=0)
    moderate_count: int = Field(ge=0)
    severe_count: int = Field(ge=0)
    members: tuple[RadioAuditMemberResult, ...] = ()
