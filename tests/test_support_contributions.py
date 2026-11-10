from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from arrest_report_agent.bootstrap.health import create_http_application
from arrest_report_agent.config import AppSettings
from arrest_report_agent.infrastructure.postgres.schema import (
    support_contributions,
    support_exchange_rates,
    supporter_profiles,
    supporter_provider_links,
)
from arrest_report_agent.support_contributions import (
    BuyMeACoffeeWebhookAuthenticationError,
    BuyMeACoffeeWebhookPayloadError,
    BuyMeACoffeeWebhookReceiver,
    ContributionEvent,
    ContributionEventKind,
    ContributionIngestResult,
    ContributionNormalizationWorker,
    ContributionStatus,
    ContributionToNormalize,
    ExchangeRate,
    ExchangeRateError,
    FrankfurterExchangeRateClient,
)

SECRET = "buy-me-a-coffee-signing-secret"


def webhook_settings() -> AppSettings:
    return AppSettings(
        _env_file=None,
        DISCORD_TOKEN="discord",
        OPENAI_API_KEY="openai",
        BUY_ME_A_COFFEE_WEBHOOK_SECRET=SECRET,
    )


def donation_payload(
    *,
    event_id: int = 1234,
    event_type: str = "donation.created",
    live_mode: bool = True,
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "type": event_type,
        "live_mode": live_mode,
        "created": 1_719_825_600,
        "attempt": 1,
        "data": {
            "id": 98765,
            "object": "payment",
            "transaction_id": "pi_3QxxxxxYyyyyy",
            "status": "refunded" if event_type == "donation.refunded" else "succeeded",
            "refunded": "true" if event_type == "donation.refunded" else "false",
            "amount": 15.0,
            "coffee_count": 3,
            "coffee_price": 5.0,
            "currency": "usd",
            "total_amount_charged": 15.0,
            "application_fee": 0.75,
            "supporter_name": "Alex",
            "supporter_name_type": "default",
            "supporter_id": 42,
            "supporter_email": "private@example.com",
            "support_note": "This must not be persisted",
            "note_hidden": "true",
            "created_at": 1_719_825_500,
            "refunded_at": 1_719_825_650 if event_type == "donation.refunded" else None,
        },
    }


def encode(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()


def signature(body: bytes, *, prefixed: bool = False) -> str:
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}" if prefixed else digest


class FakeContributionRepository:
    def __init__(self) -> None:
        self.events: list[ContributionEvent] = []
        self.keys: set[str] = set()

    async def record(self, event: ContributionEvent) -> ContributionIngestResult:
        self.events.append(event)
        if event.provider_event_key in self.keys:
            return ContributionIngestResult(False, False)
        self.keys.add(event.provider_event_key)
        return ContributionIngestResult(True, event.live_mode)


@pytest.mark.asyncio
async def test_disabled_support_authenticates_without_storing_or_waking_processing() -> None:
    from unittest.mock import AsyncMock, Mock

    repository = FakeContributionRepository()
    gate = AsyncMock(return_value=False)
    wake = Mock()
    receiver = BuyMeACoffeeWebhookReceiver(webhook_settings(), repository, wake, is_enabled=gate)
    body = encode(donation_payload())
    with pytest.raises(BuyMeACoffeeWebhookAuthenticationError):
        await receiver.receive(body, signature="invalid")
    gate.assert_not_awaited()
    result = await receiver.receive(body, signature=signature(body))
    assert result.status == "ignored"
    assert repository.events == []
    wake.assert_not_called()
    gate.return_value = True
    assert (await receiver.receive(body, signature=signature(body))).status == "accepted"
    assert len(repository.events) == 1
    wake.assert_called_once()


@pytest.mark.asyncio
async def test_normalizer_pauses_without_accessing_history_when_support_is_disabled() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    repository = SimpleNamespace(due_for_normalization=AsyncMock(return_value=()))
    gate = AsyncMock(return_value=False)
    worker = ContributionNormalizationWorker(repository, SimpleNamespace(), is_enabled=gate)
    await worker.process_once()
    repository.due_for_normalization.assert_not_awaited()
    gate.return_value = True
    await worker.process_once()
    repository.due_for_normalization.assert_awaited_once()


@pytest.mark.asyncio
async def test_buy_me_a_coffee_receiver_translates_signed_donation_without_private_fields() -> None:
    repository = FakeContributionRepository()
    receiver = BuyMeACoffeeWebhookReceiver(
        webhook_settings(),
        repository,  # type: ignore[arg-type]
    )
    body = encode(donation_payload())

    acceptance = await receiver.receive(body, signature=signature(body))

    assert acceptance.status == "accepted"
    assert acceptance.inserted is True
    event = repository.events[0]
    assert event.provider == "buy_me_a_coffee"
    assert event.provider_event_id == "1234"
    assert event.provider_contribution_id == "98765"
    assert event.event_kind is ContributionEventKind.CREATED
    assert event.status is ContributionStatus.SUCCEEDED
    assert event.provider_supporter_id == "42"
    assert event.supporter_name == "Alex"
    assert event.original_amount == Decimal("15.0")
    assert event.original_currency == "USD"
    assert event.coffee_count == 3
    assert event.contributed_at == datetime.fromtimestamp(1_719_825_500, UTC)
    assert not hasattr(event, "supporter_email")
    assert not hasattr(event, "support_note")


@pytest.mark.asyncio
async def test_receiver_is_idempotent_and_accepts_optional_signature_prefix() -> None:
    repository = FakeContributionRepository()
    receiver = BuyMeACoffeeWebhookReceiver(
        webhook_settings(),
        repository,  # type: ignore[arg-type]
    )
    body = encode(donation_payload())

    first = await receiver.receive(body, signature=signature(body, prefixed=True))
    duplicate = await receiver.receive(body, signature=signature(body))

    assert first.status == "accepted"
    assert duplicate.status == "duplicate"
    assert duplicate.inserted is False


@pytest.mark.asyncio
async def test_receiver_maps_refunds_and_keeps_dashboard_tests_out_of_live_totals() -> None:
    repository = FakeContributionRepository()
    receiver = BuyMeACoffeeWebhookReceiver(
        webhook_settings(),
        repository,  # type: ignore[arg-type]
    )
    refund_body = encode(donation_payload(event_id=2234, event_type="donation.refunded"))
    test_body = encode(donation_payload(event_id=1, live_mode=False))

    refund = await receiver.receive(refund_body, signature=signature(refund_body))
    dashboard_test = await receiver.receive(test_body, signature=signature(test_body))

    assert refund.status == "accepted"
    assert repository.events[0].status is ContributionStatus.REFUNDED
    assert repository.events[0].refunded_at == datetime.fromtimestamp(1_719_825_650, UTC)
    assert dashboard_test.status == "test"
    assert repository.events[1].live_mode is False


@pytest.mark.asyncio
async def test_receiver_rejects_bad_signatures_and_malformed_supported_payloads() -> None:
    repository = FakeContributionRepository()
    receiver = BuyMeACoffeeWebhookReceiver(
        webhook_settings(),
        repository,  # type: ignore[arg-type]
    )
    body = encode(donation_payload())
    invalid_payload = donation_payload(event_id=7788)
    invalid_payload["data"] = {"id": 123, "currency": "USD"}
    invalid_body = encode(invalid_payload)

    with pytest.raises(BuyMeACoffeeWebhookAuthenticationError):
        await receiver.receive(body, signature="wrong")
    with pytest.raises(BuyMeACoffeeWebhookPayloadError):
        await receiver.receive(invalid_body, signature=signature(invalid_body))
    assert repository.events == []


@pytest.mark.asyncio
async def test_receiver_never_substitutes_card_charge_for_missing_contribution_amount() -> None:
    repository = FakeContributionRepository()
    receiver = BuyMeACoffeeWebhookReceiver(
        webhook_settings(),
        repository,  # type: ignore[arg-type]
    )
    payload = donation_payload(event_id=7789)
    assert isinstance(payload["data"], dict)
    payload["data"].pop("amount")
    body = encode(payload)

    with pytest.raises(BuyMeACoffeeWebhookPayloadError):
        await receiver.receive(body, signature=signature(body))

    assert repository.events == []


@pytest.mark.asyncio
async def test_receiver_acknowledges_unsubscribed_event_types_without_persisting() -> None:
    repository = FakeContributionRepository()
    receiver = BuyMeACoffeeWebhookReceiver(
        webhook_settings(),
        repository,  # type: ignore[arg-type]
    )
    body = encode(donation_payload(event_type="membership.started"))

    acceptance = await receiver.receive(body, signature=signature(body))

    assert acceptance.status == "ignored"
    assert repository.events == []


@pytest.mark.asyncio
async def test_support_webhook_http_endpoint_is_public_but_requires_provider_signature() -> None:
    async def healthy() -> dict[str, object]:
        return {"ok": True}

    repository = FakeContributionRepository()
    receiver = BuyMeACoffeeWebhookReceiver(
        webhook_settings(),
        repository,  # type: ignore[arg-type]
    )
    client = TestClient(
        TestServer(create_http_application(healthy, None, None, receiver))
    )
    await client.start_server()
    try:
        body = encode(donation_payload())
        unsigned = await client.post(
            "/webhooks/support/buy-me-a-coffee",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        assert unsigned.status == 401

        accepted = await client.post(
            "/webhooks/support/buy-me-a-coffee",
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-signature-sha256": signature(body),
            },
        )
        assert accepted.status == 200
        assert await accepted.json() == {"status": "accepted"}
    finally:
        await client.close()


def test_contribution_event_key_separates_live_and_dashboard_events() -> None:
    common = ContributionEvent(
        provider="buy_me_a_coffee",
        provider_event_id="1",
        provider_contribution_id="98765",
        event_kind=ContributionEventKind.CREATED,
        status=ContributionStatus.SUCCEEDED,
        live_mode=True,
        attempt=1,
        event_timestamp=datetime(2026, 9, 10, tzinfo=UTC),
        payload_sha256="a" * 64,
        transaction_id=None,
        provider_supporter_id=None,
        supporter_name=None,
        supporter_name_type=None,
        original_amount=Decimal("5"),
        original_currency="USD",
        coffee_count=1,
        coffee_price=Decimal("5"),
        total_amount_charged=Decimal("5"),
        application_fee=None,
        contributed_at=datetime(2026, 9, 10, tzinfo=UTC),
        refunded_at=None,
    )

    assert common.provider_event_key != replace(common, live_mode=False).provider_event_key
    assert common.provider_event_key == replace(common, provider_event_id="999").provider_event_key
    assert common.provider_event_key != replace(
        common,
        event_kind=ContributionEventKind.REFUNDED,
    ).provider_event_key


@pytest.mark.asyncio
async def test_receiver_wakes_normalizer_only_for_changed_live_contributions() -> None:
    repository = FakeContributionRepository()
    wakes = 0

    def wake() -> None:
        nonlocal wakes
        wakes += 1

    receiver = BuyMeACoffeeWebhookReceiver(
        webhook_settings(),
        repository,  # type: ignore[arg-type]
        wake,
    )
    body = encode(donation_payload())

    await receiver.receive(body, signature=signature(body))
    await receiver.receive(body, signature=signature(body))

    assert wakes == 1


@pytest.mark.asyncio
async def test_frankfurter_client_reads_historical_rate_and_effective_date() -> None:
    async def rate(request: web.Request) -> web.Response:
        assert request.match_info == {"base": "CLP", "quote": "USD"}
        assert request.query["date"] == "2026-09-06"
        return web.json_response(
            {"date": "2026-09-04", "base": "CLP", "quote": "USD", "rate": 0.00104}
        )

    app = web.Application()
    app.router.add_get("/v2/rate/{base}/{quote}", rate)
    server = TestServer(app)
    await server.start_server()
    client = FrankfurterExchangeRateClient(base_url=str(server.make_url("/")))
    try:
        result = await client.get_rate(
            base_currency="clp",
            quote_currency="usd",
            requested_date=date(2026, 9, 6),
        )
    finally:
        await client.close()
        await server.close()

    assert result == ExchangeRate(
        source="frankfurter",
        requested_date=date(2026, 9, 6),
        effective_date=date(2026, 9, 4),
        base_currency="CLP",
        quote_currency="USD",
        rate=Decimal("0.00104"),
    )


@pytest.mark.asyncio
async def test_frankfurter_client_classifies_unknown_currency_as_terminal() -> None:
    async def rate(_request: web.Request) -> web.Response:
        return web.json_response({"message": "not found"}, status=404)

    app = web.Application()
    app.router.add_get("/v2/rate/{base}/{quote}", rate)
    server = TestServer(app)
    await server.start_server()
    client = FrankfurterExchangeRateClient(base_url=str(server.make_url("/")))
    try:
        with pytest.raises(ExchangeRateError) as captured:
            await client.get_rate(
                base_currency="ZZZ",
                quote_currency="USD",
                requested_date=date(2026, 9, 6),
            )
    finally:
        await client.close()
        await server.close()

    assert captured.value.retryable is False


class FakeNormalizationRepository:
    def __init__(self, contribution: ContributionToNormalize) -> None:
        self.contribution = contribution
        self.cached: ExchangeRate | None = None
        self.saved: list[ExchangeRate] = []
        self.normalized: list[tuple[Decimal, ExchangeRate]] = []
        self.failures: list[tuple[bool, str]] = []

    async def due_for_normalization(self) -> tuple[ContributionToNormalize, ...]:
        return (self.contribution,)

    async def exchange_rate(self, **_kwargs: object) -> ExchangeRate | None:
        return self.cached

    async def save_exchange_rate(self, rate: ExchangeRate) -> ExchangeRate:
        self.saved.append(rate)
        self.cached = rate
        return rate

    async def mark_normalized(
        self,
        _contribution_id: object,
        *,
        normalized_amount_usd: Decimal,
        rate: ExchangeRate,
    ) -> None:
        self.normalized.append((normalized_amount_usd, rate))

    async def mark_normalization_failed(
        self,
        _contribution_id: object,
        *,
        attempts: int,
        error: str,
        retryable: bool,
    ) -> None:
        assert attempts == self.contribution.attempts
        self.failures.append((retryable, error))


class FakeExchangeRateClient:
    def __init__(self, result: ExchangeRate | Exception) -> None:
        self.result = result
        self.calls = 0

    async def get_rate(self, **_kwargs: object) -> ExchangeRate:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_normalizer_converts_once_and_reuses_persistent_rate_cache() -> None:
    contribution = ContributionToNormalize(
        id=uuid4(),
        provider="buy_me_a_coffee",
        provider_contribution_id="9",
        original_amount=Decimal("10000"),
        original_currency="CLP",
        contributed_at=datetime(2026, 9, 6, 23, 0, tzinfo=UTC),
        attempts=0,
    )
    fetched = ExchangeRate(
        source="frankfurter",
        requested_date=date(2026, 9, 6),
        effective_date=date(2026, 9, 4),
        base_currency="CLP",
        quote_currency="USD",
        rate=Decimal("0.00104"),
    )
    repository = FakeNormalizationRepository(contribution)
    client = FakeExchangeRateClient(fetched)
    worker = ContributionNormalizationWorker(
        repository,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
    )

    await worker.process_once()
    await worker.process_once()

    assert client.calls == 1
    assert repository.saved == [fetched]
    assert repository.normalized == [
        (Decimal("10.400000"), fetched),
        (Decimal("10.400000"), fetched),
    ]


@pytest.mark.asyncio
async def test_normalizer_persists_retryability_without_losing_contribution() -> None:
    contribution = ContributionToNormalize(
        id=uuid4(),
        provider="buy_me_a_coffee",
        provider_contribution_id="10",
        original_amount=Decimal("20"),
        original_currency="XYZ",
        contributed_at=datetime(2026, 9, 10, tzinfo=UTC),
        attempts=2,
    )
    repository = FakeNormalizationRepository(contribution)
    client = FakeExchangeRateClient(ExchangeRateError("unsupported", retryable=False))
    worker = ContributionNormalizationWorker(
        repository,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
    )

    await worker.process_once()

    assert repository.normalized == []
    assert repository.failures == [(False, "ExchangeRateError: unsupported")]


def test_support_schema_keeps_money_identity_and_normalization_separate() -> None:
    contribution_columns = set(support_contributions.c.keys())
    assert {
        "original_amount",
        "original_currency",
        "normalized_amount_usd",
        "exchange_rate_to_usd",
        "supporter_profile_id",
    } <= contribution_columns
    assert {"requested_date", "effective_date", "rate"} <= set(
        support_exchange_rates.c.keys()
    )
    assert "discord_user_id" in supporter_profiles.c
    assert {"provider", "provider_supporter_id", "profile_id"} <= set(
        supporter_provider_links.c.keys()
    )
