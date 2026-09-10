"""Authenticated Buy Me a Coffee webhook adapter."""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from arrest_report_agent.config import AppSettings
from arrest_report_agent.config.logging import log_event

from .models import ContributionEvent, ContributionEventKind, ContributionStatus
from .repository import SupportContributionRepository

LOGGER = logging.getLogger(__name__)
PROVIDER = "buy_me_a_coffee"
SUPPORTED_EVENTS = {
    "donation.created": ContributionEventKind.CREATED,
    "donation.refunded": ContributionEventKind.REFUNDED,
}


class BuyMeACoffeeWebhookAuthenticationError(ValueError):
    """Raised when a webhook signature is absent or invalid."""


class BuyMeACoffeeWebhookPayloadError(ValueError):
    """Raised when a signed webhook has an invalid supported payload."""


class _BuyMeACoffeeModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, str_strip_whitespace=True)


class _EventEnvelope(_BuyMeACoffeeModel):
    event_id: int = Field(ge=1)
    type: str = Field(min_length=1, max_length=80)
    live_mode: bool
    created: int = Field(gt=0)
    attempt: int = Field(ge=1)
    data: dict[str, Any]


class _DonationData(_BuyMeACoffeeModel):
    id: int = Field(gt=0)
    transaction_id: str | None = Field(default=None, max_length=255)
    status: str | None = Field(default=None, max_length=30)
    refunded: bool | str | None = None
    amount: Decimal | None = Field(default=None, ge=0)
    coffee_count: int | None = Field(default=None, ge=0)
    coffee_price: Decimal | None = Field(default=None, ge=0)
    currency: str = Field(min_length=3, max_length=3)
    total_amount_charged: Decimal | None = Field(default=None, ge=0)
    application_fee: Decimal | None = Field(default=None, ge=0)
    supporter_name: str | None = Field(default=None, max_length=200)
    supporter_name_type: str | None = Field(default=None, max_length=50)
    supporter_id: int | None = Field(default=None, gt=0)
    created_at: int | None = Field(default=None, gt=0)
    refunded_at: int | None = Field(default=None, gt=0)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        normalized = value.upper()
        if not normalized.isalpha():
            raise ValueError("currency must be a three-letter code")
        return normalized

    @field_validator("refunded", mode="before")
    @classmethod
    def normalize_refunded(cls, value: object) -> bool | None:
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "false"}:
                return normalized == "true"
        raise ValueError("refunded must be a boolean")


@dataclass(frozen=True, slots=True)
class BuyMeACoffeeWebhookAcceptance:
    status: str
    inserted: bool = False


class BuyMeACoffeeWebhookReceiver:
    """Verify provider signatures and translate donations into the shared domain."""

    def __init__(
        self,
        settings: AppSettings,
        repository: SupportContributionRepository,
        wake_normalizer: Callable[[], None] | None = None,
    ) -> None:
        if not settings.buy_me_a_coffee_webhook_enabled:
            raise ValueError(
                "Buy Me a Coffee receiver requires BUY_ME_A_COFFEE_WEBHOOK_SECRET"
            )
        secret = settings.buy_me_a_coffee_webhook_secret
        assert secret is not None
        self._secret = secret.get_secret_value().encode()
        self.repository = repository
        self._wake_normalizer = wake_normalizer

    async def receive(
        self,
        raw_body: bytes,
        *,
        signature: str,
    ) -> BuyMeACoffeeWebhookAcceptance:
        self._verify_signature(raw_body, signature)
        try:
            envelope = _EventEnvelope.model_validate_json(raw_body)
        except ValidationError as error:
            raise BuyMeACoffeeWebhookPayloadError("Invalid webhook envelope") from error

        event_kind = SUPPORTED_EVENTS.get(envelope.type)
        if event_kind is None:
            log_event(
                LOGGER,
                "info",
                "support_webhook_ignored",
                provider=PROVIDER,
                provider_event_id=envelope.event_id,
                event_type=envelope.type,
                reason="unsupported_event_type",
            )
            return BuyMeACoffeeWebhookAcceptance(status="ignored")

        try:
            donation = _DonationData.model_validate(envelope.data)
            event = _to_contribution_event(
                envelope,
                donation,
                event_kind=event_kind,
                payload_sha256=hashlib.sha256(raw_body).hexdigest(),
            )
        except (ValidationError, ValueError, OSError, OverflowError) as error:
            raise BuyMeACoffeeWebhookPayloadError("Invalid donation payload") from error

        result = await self.repository.record(event)
        if result.contribution_updated and self._wake_normalizer is not None:
            self._wake_normalizer()
        status = "accepted" if result.event_inserted else "duplicate"
        if not envelope.live_mode and result.event_inserted:
            status = "test"
        log_event(
            LOGGER,
            "info",
            "support_webhook_received",
            provider=PROVIDER,
            provider_event_id=envelope.event_id,
            event_type=envelope.type,
            live_mode=envelope.live_mode,
            attempt=envelope.attempt,
            status=status,
            contribution_updated=result.contribution_updated,
        )
        return BuyMeACoffeeWebhookAcceptance(
            status=status,
            inserted=result.event_inserted,
        )

    def _verify_signature(self, raw_body: bytes, signature: str) -> None:
        candidate = signature.strip().lower()
        if candidate.startswith("sha256="):
            candidate = candidate.removeprefix("sha256=")
        expected = hmac.new(self._secret, raw_body, hashlib.sha256).hexdigest()
        if len(candidate) != len(expected) or not hmac.compare_digest(expected, candidate):
            raise BuyMeACoffeeWebhookAuthenticationError("Invalid webhook signature")


def _to_contribution_event(
    envelope: _EventEnvelope,
    donation: _DonationData,
    *,
    event_kind: ContributionEventKind,
    payload_sha256: str,
) -> ContributionEvent:
    amount = donation.amount
    if amount is None:
        raise ValueError("donation amount is required")
    event_timestamp = _from_unix(envelope.created)
    contributed_at = _from_unix(donation.created_at or envelope.created)
    is_refunded = (
        event_kind is ContributionEventKind.REFUNDED
        or donation.refunded is True
        or (donation.status or "").casefold() == ContributionStatus.REFUNDED.value
    )
    refunded_at = (
        _from_unix(donation.refunded_at)
        if donation.refunded_at is not None
        else event_timestamp if is_refunded else None
    )
    return ContributionEvent(
        provider=PROVIDER,
        provider_event_id=str(envelope.event_id),
        provider_contribution_id=str(donation.id),
        event_kind=event_kind,
        status=(ContributionStatus.REFUNDED if is_refunded else ContributionStatus.SUCCEEDED),
        live_mode=envelope.live_mode,
        attempt=envelope.attempt,
        event_timestamp=event_timestamp,
        payload_sha256=payload_sha256,
        transaction_id=donation.transaction_id,
        provider_supporter_id=(str(donation.supporter_id) if donation.supporter_id else None),
        supporter_name=donation.supporter_name,
        supporter_name_type=donation.supporter_name_type,
        original_amount=amount,
        original_currency=donation.currency,
        coffee_count=donation.coffee_count,
        coffee_price=donation.coffee_price,
        total_amount_charged=donation.total_amount_charged,
        application_fee=donation.application_fee,
        contributed_at=contributed_at,
        refunded_at=refunded_at,
    )


def _from_unix(value: int) -> datetime:
    return datetime.fromtimestamp(value, UTC)
