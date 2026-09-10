"""Provider-neutral contribution events persisted by the application."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID


class ContributionEventKind(StrEnum):
    CREATED = "created"
    REFUNDED = "refunded"


class ContributionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    REFUNDED = "refunded"


class ContributionNormalizationStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    RETRY = "retry"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ContributionEvent:
    provider: str
    provider_event_id: str
    provider_contribution_id: str
    event_kind: ContributionEventKind
    status: ContributionStatus
    live_mode: bool
    attempt: int
    event_timestamp: datetime
    payload_sha256: str
    transaction_id: str | None
    provider_supporter_id: str | None
    supporter_name: str | None
    supporter_name_type: str | None
    original_amount: Decimal
    original_currency: str
    coffee_count: int | None
    coffee_price: Decimal | None
    total_amount_charged: Decimal | None
    application_fee: Decimal | None
    contributed_at: datetime
    refunded_at: datetime | None

    @property
    def provider_event_key(self) -> str:
        # Provider delivery IDs may change between retry attempts. The business
        # event is one lifecycle transition for one provider contribution.
        identity = (
            f"{self.provider}:{self.live_mode}:{self.provider_contribution_id}:"
            f"{self.event_kind.value}"
        )
        return hashlib.sha256(identity.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ContributionToNormalize:
    id: UUID
    provider: str
    provider_contribution_id: str
    original_amount: Decimal
    original_currency: str
    contributed_at: datetime
    attempts: int


@dataclass(frozen=True, slots=True)
class ExchangeRate:
    source: str
    requested_date: date
    effective_date: date
    base_currency: str
    quote_currency: str
    rate: Decimal


@dataclass(frozen=True, slots=True)
class SupporterProfile:
    id: UUID
    discord_user_id: int
    display_name: str | None
    mention_enabled: bool
