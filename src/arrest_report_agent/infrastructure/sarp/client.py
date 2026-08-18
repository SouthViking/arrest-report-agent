"""Lifecycle-safe asynchronous client for the documented SARP API contract."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from email.utils import parsedate_to_datetime
from typing import Any, Literal, TypeVar
from urllib.parse import urlsplit

import aiohttp
from pydantic import TypeAdapter, ValidationError

from arrest_report_agent import __version__
from arrest_report_agent.config import AppSettings
from arrest_report_agent.config.logging import exception_fields, log_event

from .errors import (
    SarpApiError,
    SarpClientNotOpenError,
    SarpConfigurationError,
    SarpError,
    SarpInvalidResponseError,
    SarpTransportError,
    build_api_error,
)
from .models import (
    SarpArrestDetail,
    SarpArrestPage,
    SarpCharacterPossessions,
    SarpCharacterPreview,
    SarpCharacterSearchResult,
    SarpEmergencyCallPage,
    SarpEnvelope,
    SarpFactionArrestSearchPayload,
    SarpFactionDetail,
    SarpFactionRank,
    SarpFinePage,
    SarpGlobalMetrics,
    SarpPenalCodePayload,
    SarpRateLimit,
    SarpResponse,
    SarpWarrantPage,
)

LOGGER = logging.getLogger(__name__)

HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
JsonBody = Mapping[str, object] | Sequence[object]
QueryValue = str | int | float | bool
Sleep = Callable[[float], Awaitable[None]]
PayloadT = TypeVar("PayloadT")

SAFE_RETRY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
DEFAULT_BASE_URL = "https://api.gta-rol.com/api"
PENAL_CODE_PAYLOAD_ADAPTER = TypeAdapter(SarpPenalCodePayload)
CHARACTER_SEARCH_PAYLOAD_ADAPTER = TypeAdapter(list[SarpCharacterSearchResult])
CHARACTER_PREVIEW_PAYLOAD_ADAPTER = TypeAdapter(SarpCharacterPreview)
CHARACTER_POSSESSIONS_PAYLOAD_ADAPTER = TypeAdapter(SarpCharacterPossessions)
FINE_PAGE_PAYLOAD_ADAPTER = TypeAdapter(SarpFinePage)
WARRANT_PAGE_PAYLOAD_ADAPTER = TypeAdapter(SarpWarrantPage)
ARREST_PAGE_PAYLOAD_ADAPTER = TypeAdapter(SarpArrestPage)
ARREST_DETAIL_PAYLOAD_ADAPTER = TypeAdapter(SarpArrestDetail)
FACTION_ARREST_SEARCH_PAYLOAD_ADAPTER = TypeAdapter(SarpFactionArrestSearchPayload)
EMERGENCY_CALL_PAGE_PAYLOAD_ADAPTER = TypeAdapter(SarpEmergencyCallPage)
FACTION_DETAIL_PAYLOAD_ADAPTER = TypeAdapter(SarpFactionDetail)
FACTION_RANKS_PAYLOAD_ADAPTER = TypeAdapter(list[SarpFactionRank])
GLOBAL_METRICS_PAYLOAD_ADAPTER = TypeAdapter(SarpGlobalMetrics)


@dataclass(frozen=True, slots=True)
class SarpClientConfig:
    """Validated process configuration for one reusable SARP client."""

    token: str | None = field(default=None, repr=False)
    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: float = 10.0
    max_retries: int = 2
    retry_backoff_seconds: float = 0.5
    max_retry_delay_seconds: float = 30.0

    def __post_init__(self) -> None:
        normalized_token = self.token.strip() if self.token is not None else None
        if normalized_token == "":
            normalized_token = None
        if normalized_token is not None and any(character in normalized_token for character in "\r\n"):
            raise SarpConfigurationError("SARP_API_TOKEN contains invalid control characters")
        object.__setattr__(self, "token", normalized_token)

        normalized_url = _normalize_base_url(self.base_url)
        object.__setattr__(self, "base_url", normalized_url)
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise SarpConfigurationError("SARP_API_TIMEOUT_SECONDS must be positive")
        if not 0 <= self.max_retries <= 10:
            raise SarpConfigurationError("SARP_API_MAX_RETRIES must be between 0 and 10")
        if not math.isfinite(self.retry_backoff_seconds) or self.retry_backoff_seconds <= 0:
            raise SarpConfigurationError("SARP API retry backoff must be positive")
        if not math.isfinite(self.max_retry_delay_seconds) or self.max_retry_delay_seconds <= 0:
            raise SarpConfigurationError("SARP API maximum retry delay must be positive")

    @classmethod
    def from_settings(cls, settings: AppSettings) -> SarpClientConfig:
        token = (
            settings.sarp_api_token.get_secret_value()
            if settings.sarp_api_token is not None
            else None
        )
        return cls(
            token=token,
            base_url=settings.sarp_api_base_url,
            timeout_seconds=settings.sarp_api_timeout_seconds,
            max_retries=settings.sarp_api_max_retries,
        )


class SarpClient:
    """Own authentication, transport, retries, validation and safe logging for SARP."""

    def __init__(
        self,
        config: SarpClientConfig,
        *,
        session: aiohttp.ClientSession | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.config = config
        self._session = session
        self._owns_session = session is None
        self._sleep = sleep

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            raise SarpClientNotOpenError("SARP client is not open")
        return self._session

    async def open(self) -> None:
        if self._session is not None:
            if self._session.closed:
                raise SarpClientNotOpenError("Injected SARP session is already closed")
            return
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.config.timeout_seconds),
        )
        log_event(
            LOGGER,
            "info",
            "sarp_client_opened",
            authentication="bearer" if self.config.token is not None else "public",
            base_url=self.config.base_url,
        )

    async def close(self) -> None:
        session = self._session
        self._session = None
        if session is not None and self._owns_session and not session.closed:
            await session.close()
        log_event(LOGGER, "info", "sarp_client_closed")

    async def __aenter__(self) -> SarpClient:
        await self.open()
        return self

    async def __aexit__(
        self,
        _error_type: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: object | None,
    ) -> None:
        await self.close()

    async def request(
        self,
        method: HttpMethod,
        path: str,
        *,
        payload_adapter: TypeAdapter[PayloadT],
        params: Mapping[str, QueryValue | None] | None = None,
        json_body: JsonBody | None = None,
        form_data: aiohttp.FormData | None = None,
        expected_statuses: Collection[int] | None = None,
    ) -> SarpResponse[PayloadT]:
        """Execute one endpoint request without embedding endpoint-specific behavior."""

        normalized_method = method.upper()
        normalized_path = _normalize_path(path)
        if json_body is not None and form_data is not None:
            raise ValueError("json_body and form_data cannot be sent together")

        attempts = self.config.max_retries + 1
        for attempt in range(1, attempts + 1):
            started_at = time.monotonic()
            try:
                response = await self._request_once(
                    normalized_method,
                    normalized_path,
                    payload_adapter=payload_adapter,
                    params=params,
                    json_body=json_body,
                    form_data=form_data,
                    expected_statuses=expected_statuses,
                )
                log_event(
                    LOGGER,
                    "debug",
                    "sarp_request_succeeded",
                    method=normalized_method,
                    path=normalized_path,
                    status_code=response.http_code,
                    source=response.source or "unknown",
                    attempt=attempt,
                    duration_ms=round((time.monotonic() - started_at) * 1000),
                    request_id=response.request_id or "unknown",
                )
                return response
            except SarpError as error:
                if self._should_retry(normalized_method, error, attempt):
                    delay = self._retry_delay(error, attempt)
                    log_event(
                        LOGGER,
                        "warning",
                        "sarp_request_retrying",
                        method=normalized_method,
                        path=normalized_path,
                        attempt=attempt,
                        retry_delay_seconds=delay,
                        **exception_fields(error),
                    )
                    await self._sleep(delay)
                    continue
                log_event(
                    LOGGER,
                    "warning",
                    "sarp_request_failed",
                    method=normalized_method,
                    path=normalized_path,
                    attempts=attempt,
                    retryable=error.retryable,
                    **exception_fields(error),
                )
                raise

        raise AssertionError("SARP retry loop exited unexpectedly")

    async def get_penal_code(self) -> SarpPenalCodePayload:
        """Fetch the complete structured police penal code."""

        response = await self.request(
            "GET",
            "/police/penal-code",
            payload_adapter=PENAL_CODE_PAYLOAD_ADAPTER,
        )
        return response.payload

    async def get_global_metrics(self) -> SarpGlobalMetrics:
        """Fetch public server-wide metrics, including connected accounts."""

        response = await self.request(
            "GET",
            "/public/global-metrics",
            payload_adapter=GLOBAL_METRICS_PAYLOAD_ADAPTER,
        )
        return response.payload

    async def get_faction_detail(self, faction_id: int) -> SarpFactionDetail:
        """Fetch one complete faction roster with its live connection state."""

        _require_positive_id(faction_id, "faction_id")
        response = await self.request(
            "GET",
            f"/my-faction/faction/{faction_id}",
            payload_adapter=FACTION_DETAIL_PAYLOAD_ADAPTER,
        )
        return response.payload

    async def get_faction_ranks(self, faction_id: int) -> list[SarpFactionRank]:
        """Fetch the faction's ordered rank catalog."""

        _require_positive_id(faction_id, "faction_id")
        response = await self.request(
            "GET",
            f"/my-faction/ranks/{faction_id}",
            payload_adapter=FACTION_RANKS_PAYLOAD_ADAPTER,
        )
        return response.payload

    async def search_characters(self, name: str) -> list[SarpCharacterSearchResult]:
        """Search character names using the documented prefix endpoint."""

        normalized_name = name.strip()
        if not normalized_name or len(normalized_name) > 100:
            raise ValueError("Character search name must contain between 1 and 100 characters")
        response = await self.request(
            "GET",
            "/character/search",
            params={"name": normalized_name},
            payload_adapter=CHARACTER_SEARCH_PAYLOAD_ADAPTER,
        )
        return response.payload

    async def get_character_preview(self, character_id: int) -> SarpCharacterPreview:
        """Fetch one public character profile."""

        _require_positive_id(character_id, "character_id")
        response = await self.request(
            "GET",
            f"/character/preview/{character_id}",
            payload_adapter=CHARACTER_PREVIEW_PAYLOAD_ADAPTER,
        )
        return response.payload

    async def get_character_possessions(self, character_id: int) -> SarpCharacterPossessions:
        """Fetch the public registered possessions of one character."""

        _require_positive_id(character_id, "character_id")
        response = await self.request(
            "GET",
            f"/character/preview/{character_id}/possessions",
            payload_adapter=CHARACTER_POSSESSIONS_PAYLOAD_ADAPTER,
        )
        return response.payload

    async def list_unpaid_fines(
        self,
        character_id: int,
        *,
        page: int = 1,
        limit: int = 100,
    ) -> SarpFinePage:
        """List unpaid fines for one character."""

        _require_positive_id(character_id, "character_id")
        _validate_page(page, limit)
        response = await self.request(
            "GET",
            "/police/fines",
            params={
                "character_id": character_id,
                "is_paid": 0,
                "page": page,
                "limit": limit,
            },
            payload_adapter=FINE_PAGE_PAYLOAD_ADAPTER,
        )
        return response.payload

    async def list_active_warrants(
        self,
        character_id: int,
        *,
        page: int = 1,
        limit: int = 25,
    ) -> SarpWarrantPage:
        """List active, non-confidential warrants for one suspect."""

        _require_positive_id(character_id, "character_id")
        _validate_page(page, limit)
        response = await self.request(
            "GET",
            "/police/warrants",
            params={
                "suspect_id": character_id,
                "status": 1,
                "page": page,
                "limit": limit,
            },
            payload_adapter=WARRANT_PAGE_PAYLOAD_ADAPTER,
        )
        return response.payload

    async def list_arrests(
        self,
        character_id: int,
        *,
        page: int = 1,
        limit: int = 3,
    ) -> SarpArrestPage:
        """List recent arrest records for one suspect."""

        _require_positive_id(character_id, "character_id")
        _validate_page(page, limit)
        response = await self.request(
            "GET",
            "/police/arrests",
            params={"suspect_id": character_id, "page": page, "limit": limit},
            payload_adapter=ARREST_PAGE_PAYLOAD_ADAPTER,
        )
        return response.payload

    async def search_faction_arrests(
        self,
        faction_id: int,
        *,
        suspect_name: str | None = None,
        officer_name: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        page: int = 1,
        limit: int = 20,
        sort_field: str | None = None,
        sort_order: Literal["asc", "desc"] = "desc",
    ) -> SarpFactionArrestSearchPayload:
        """Search a police faction's arrest reports using the documented filters."""

        _require_positive_id(faction_id, "faction_id")
        _validate_page(page, limit, max_limit=50)
        normalized_suspect = _normalize_optional_search(suspect_name, "suspect_name")
        normalized_officer = _normalize_optional_search(officer_name, "officer_name")
        normalized_sort_field = _normalize_sort_field(sort_field)
        if sort_order not in {"asc", "desc"}:
            raise ValueError("sort_order must be 'asc' or 'desc'")
        if date_from is not None and date_to is not None and date_from > date_to:
            raise ValueError("date_from cannot be after date_to")
        response = await self.request(
            "GET",
            f"/my-faction/arrests/{faction_id}",
            params={
                "page": page,
                "limit": limit,
                "dateFrom": date_from.isoformat() if date_from is not None else None,
                "dateTo": date_to.isoformat() if date_to is not None else None,
                "suspectSearch": normalized_suspect,
                "agentSearch": normalized_officer,
                "sortField": normalized_sort_field,
                "sortOrder": 1 if sort_order == "asc" else -1,
            },
            payload_adapter=FACTION_ARREST_SEARCH_PAYLOAD_ADAPTER,
        )
        return response.payload

    async def get_arrest(self, arrest_id: int) -> SarpArrestDetail:
        """Fetch the current detail and charges for one arrest report."""

        _require_positive_id(arrest_id, "arrest_id")
        response = await self.request(
            "GET",
            f"/police/arrests/{arrest_id}",
            payload_adapter=ARREST_DETAIL_PAYLOAD_ADAPTER,
        )
        return response.payload

    async def list_emergency_calls(
        self,
        *,
        page: int = 1,
        limit: int = 100,
    ) -> SarpEmergencyCallPage:
        """List the most recent police emergency calls."""

        _validate_page(page, limit)
        response = await self.request(
            "GET",
            "/police/emergency-calls",
            params={"page": page, "limit": limit},
            payload_adapter=EMERGENCY_CALL_PAGE_PAYLOAD_ADAPTER,
        )
        return response.payload

    async def _request_once(
        self,
        method: str,
        path: str,
        *,
        payload_adapter: TypeAdapter[PayloadT],
        params: Mapping[str, QueryValue | None] | None,
        json_body: JsonBody | None,
        form_data: aiohttp.FormData | None,
        expected_statuses: Collection[int] | None,
    ) -> SarpResponse[PayloadT]:
        headers = {
            "Accept": "application/json",
            "User-Agent": f"arrest-report-agent/{__version__}",
        }
        if self.config.token is not None:
            headers["Authorization"] = f"Bearer {self.config.token}"
        request_params = (
            {key: value for key, value in params.items() if value is not None}
            if params is not None
            else None
        )
        kwargs: dict[str, Any] = {
            "headers": headers,
            "params": request_params,
        }
        if json_body is not None:
            kwargs["json"] = json_body
        if form_data is not None:
            kwargs["data"] = form_data

        try:
            async with self.session.request(
                method,
                f"{self.config.base_url}{path}",
                **kwargs,
            ) as response:
                return await self._parse_response(
                    response,
                    payload_adapter=payload_adapter,
                    expected_statuses=expected_statuses,
                )
        except (aiohttp.ClientError, TimeoutError) as error:
            raise SarpTransportError(method, path) from error

    async def _parse_response(
        self,
        response: aiohttp.ClientResponse,
        *,
        payload_adapter: TypeAdapter[PayloadT],
        expected_statuses: Collection[int] | None,
    ) -> SarpResponse[PayloadT]:
        request_id = _request_id(response.headers)
        rate_limit = _rate_limit(response.headers)
        try:
            raw_payload = await response.json(content_type=None)
        except (aiohttp.ContentTypeError, ValueError) as error:
            raise SarpInvalidResponseError(
                "SARP returned a non-JSON response",
                status_code=response.status,
                request_id=request_id,
            ) from error
        try:
            envelope = SarpEnvelope.model_validate(raw_payload)
        except ValidationError as error:
            raise SarpInvalidResponseError(
                "SARP returned an invalid response envelope",
                status_code=response.status,
                request_id=request_id,
            ) from error
        if envelope.http_code != response.status:
            raise SarpInvalidResponseError(
                "SARP response status does not match httpCode",
                status_code=response.status,
                request_id=request_id,
            )

        accepted = (
            response.status in expected_statuses
            if expected_statuses is not None
            else 200 <= response.status < 300
        )
        if not accepted:
            raise build_api_error(
                status_code=response.status,
                message=envelope.message,
                source=envelope.source,
                request_id=request_id,
                rate_limit=rate_limit,
            )
        try:
            typed_payload = payload_adapter.validate_python(envelope.payload)
        except ValidationError as error:
            raise SarpInvalidResponseError(
                "SARP returned an invalid endpoint payload",
                status_code=response.status,
                request_id=request_id,
            ) from error
        return SarpResponse(
            message=envelope.message,
            http_code=envelope.http_code,
            source=envelope.source,
            payload=typed_payload,
            rate_limit=rate_limit,
            request_id=request_id,
        )

    def _should_retry(self, method: str, error: SarpError, attempt: int) -> bool:
        return (
            method in SAFE_RETRY_METHODS
            and error.retryable
            and attempt <= self.config.max_retries
        )

    def _retry_delay(self, error: SarpError, attempt: int) -> float:
        if isinstance(error, SarpApiError):
            retry_after = error.rate_limit.retry_after_seconds
            if retry_after is not None:
                return min(retry_after, self.config.max_retry_delay_seconds)
        exponential: float = self.config.retry_backoff_seconds * (2.0 ** max(attempt - 1, 0))
        return min(exponential, self.config.max_retry_delay_seconds)


def _normalize_base_url(raw_url: str) -> str:
    value = raw_url.strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SarpConfigurationError("SARP_API_BASE_URL must be an HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SarpConfigurationError("SARP_API_BASE_URL cannot contain credentials, query or fragment")
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SarpConfigurationError("SARP_API_BASE_URL must use HTTPS outside local tests")
    return value


def _normalize_path(raw_path: str) -> str:
    value = raw_path.strip()
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("SARP request path must be relative and contain no query string")
    segments = [segment for segment in parsed.path.split("/") if segment]
    if segments and segments[0].casefold() == "api":
        segments = segments[1:]
    if not segments or any(segment in {".", ".."} for segment in segments):
        raise ValueError("SARP request path is empty or unsafe")
    if any(any(character in segment for character in "\r\n") for segment in segments):
        raise ValueError("SARP request path contains invalid control characters")
    return "/" + "/".join(segments)


def _require_positive_id(value: int, label: str) -> None:
    if isinstance(value, bool) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")


def _validate_page(page: int, limit: int, *, max_limit: int = 100) -> None:
    if isinstance(page, bool) or page < 1:
        raise ValueError("page must be a positive integer")
    if isinstance(limit, bool) or not 1 <= limit <= max_limit:
        raise ValueError(f"limit must be between 1 and {max_limit}")


def _normalize_optional_search(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not 1 <= len(normalized) <= 100:
        raise ValueError(f"{field_name} must contain between 1 and 100 characters")
    return normalized


def _normalize_sort_field(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 50 or not normalized.replace("_", "").isalnum():
        raise ValueError("sort_field must be an identifier of at most 50 characters")
    return normalized


def _request_id(headers: Mapping[str, str]) -> str | None:
    return headers.get("X-Request-ID") or headers.get("Request-ID") or headers.get("CF-Ray")


def _rate_limit(headers: Mapping[str, str]) -> SarpRateLimit:
    return SarpRateLimit(
        limit=headers.get("RateLimit"),
        policy=headers.get("RateLimit-Policy"),
        reset=headers.get("RateLimit-Reset"),
        retry_after_seconds=_parse_retry_after(headers.get("Retry-After")),
    )


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            seconds = parsed.timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return None
    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)
