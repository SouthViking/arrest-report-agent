"""Historical exchange-rate provider used to normalize contributions."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal, InvalidOperation

import aiohttp

from .models import ExchangeRate

FRANKFURTER_SOURCE = "frankfurter"
FRANKFURTER_BASE_URL = "https://api.frankfurter.dev"


class ExchangeRateError(RuntimeError):
    """Base error carrying whether a later attempt may succeed."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class FrankfurterExchangeRateClient:
    """Fetch one historical currency rate from Frankfurter's public API."""

    def __init__(
        self,
        *,
        base_url: str = FRANKFURTER_BASE_URL,
        timeout_seconds: float = 10.0,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._session = session
        self._owns_session = session is None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_seconds),
                headers={"Accept": "application/json"},
            )
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()

    async def get_rate(
        self,
        *,
        base_currency: str,
        quote_currency: str,
        requested_date: date,
    ) -> ExchangeRate:
        base = _currency_code(base_currency)
        quote = _currency_code(quote_currency)
        if base == quote:
            raise ExchangeRateError("Currency pair must contain distinct currencies", retryable=False)
        url = f"{self.base_url}/v2/rate/{base}/{quote}"
        try:
            async with self.session.get(
                url,
                params={"date": requested_date.isoformat()},
            ) as response:
                body = await response.text()
                if response.status != 200:
                    retryable = response.status == 429 or response.status >= 500
                    raise ExchangeRateError(
                        f"Frankfurter returned HTTP {response.status}",
                        retryable=retryable,
                    )
        except ExchangeRateError:
            raise
        except (TimeoutError, aiohttp.ClientError) as error:
            raise ExchangeRateError("Frankfurter request failed", retryable=True) from error

        try:
            payload = json.loads(body, parse_float=Decimal)
            effective_date = date.fromisoformat(str(payload["date"]))
            response_base = _currency_code(str(payload["base"]))
            response_quote = _currency_code(str(payload["quote"]))
            rate = Decimal(payload["rate"])
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise ExchangeRateError("Frankfurter returned an invalid response", retryable=True) from error
        if response_base != base or response_quote != quote or rate <= 0:
            raise ExchangeRateError("Frankfurter returned an inconsistent rate", retryable=True)
        return ExchangeRate(
            source=FRANKFURTER_SOURCE,
            requested_date=requested_date,
            effective_date=effective_date,
            base_currency=base,
            quote_currency=quote,
            rate=rate,
        )


def _currency_code(value: str) -> str:
    normalized = value.strip().upper()
    if len(normalized) != 3 or not normalized.isalpha():
        raise ExchangeRateError("Currency must be a three-letter code", retryable=False)
    return normalized
