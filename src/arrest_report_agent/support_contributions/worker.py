"""Asynchronous contribution normalization with durable retries."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC
from decimal import ROUND_HALF_UP, Decimal

from arrest_report_agent.config.logging import exception_fields, log_event

from .exchange_rates import (
    FRANKFURTER_SOURCE,
    ExchangeRateError,
    FrankfurterExchangeRateClient,
)
from .models import ContributionToNormalize, ExchangeRate
from .repository import IDENTITY_RATE_SOURCE, USD, SupportContributionRepository

LOGGER = logging.getLogger(__name__)
POLL_SECONDS = 60.0
USD_PRECISION = Decimal("0.000001")


class ContributionNormalizationWorker:
    """Normalize pending records without delaying or retrying provider webhooks."""

    def __init__(
        self,
        repository: SupportContributionRepository,
        exchange_rates: FrankfurterExchangeRateClient,
        *,
        poll_seconds: float = POLL_SECONDS,
        is_enabled: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self.repository = repository
        self._is_enabled = is_enabled
        self.exchange_rates = exchange_rates
        self.poll_seconds = poll_seconds
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._processing = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="support-contribution-normalization")
        log_event(LOGGER, "info", "support_contribution_normalizer_started")

    def wake(self) -> None:
        self._wake.set()

    async def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self.exchange_rates.close()

    async def process_once(self) -> None:
        async with self._processing:
            if self._is_enabled is not None and not await self._is_enabled():
                return
            for contribution in await self.repository.due_for_normalization():
                await self._normalize(contribution)

    async def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                await self.process_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log_event(
                    LOGGER,
                    "error",
                    "support_contribution_normalizer_failed",
                    exc_info=error,
                    **exception_fields(error),
                )
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)

    async def _normalize(self, contribution: ContributionToNormalize) -> None:
        try:
            rate = await self._rate_for(contribution)
            normalized = (contribution.original_amount * rate.rate).quantize(
                USD_PRECISION,
                rounding=ROUND_HALF_UP,
            )
            await self.repository.mark_normalized(
                contribution.id,
                normalized_amount_usd=normalized,
                rate=rate,
            )
        except Exception as error:
            retryable = not isinstance(error, ExchangeRateError) or error.retryable
            await self.repository.mark_normalization_failed(
                contribution.id,
                attempts=contribution.attempts,
                error=f"{type(error).__name__}: {error}",
                retryable=retryable,
            )
            log_event(
                LOGGER,
                "warning" if retryable else "error",
                "support_contribution_normalization_failed",
                exc_info=error,
                provider=contribution.provider,
                provider_contribution_id=contribution.provider_contribution_id,
                original_currency=contribution.original_currency,
                retryable=retryable,
                **exception_fields(error),
            )

    async def _rate_for(self, contribution: ContributionToNormalize) -> ExchangeRate:
        requested_date = contribution.contributed_at.astimezone(UTC).date()
        if contribution.original_currency == USD:
            return ExchangeRate(
                source=IDENTITY_RATE_SOURCE,
                requested_date=requested_date,
                effective_date=requested_date,
                base_currency=USD,
                quote_currency=USD,
                rate=Decimal("1"),
            )
        cached = await self.repository.exchange_rate(
            source=FRANKFURTER_SOURCE,
            requested_date=requested_date,
            base_currency=contribution.original_currency,
            quote_currency=USD,
        )
        if cached is not None:
            return cached
        fetched = await self.exchange_rates.get_rate(
            base_currency=contribution.original_currency,
            quote_currency=USD,
            requested_date=requested_date,
        )
        return await self.repository.save_exchange_rate(fetched)
