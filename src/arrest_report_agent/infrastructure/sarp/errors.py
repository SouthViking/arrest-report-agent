"""Safe, typed failures raised by the SARP API client."""

from __future__ import annotations

from .models import SarpRateLimit


class SarpError(RuntimeError):
    """Base class for every SARP integration failure."""

    code = "sarp_error"
    retryable = False


class SarpConfigurationError(SarpError, ValueError):
    """The client received an unsafe or incomplete configuration."""

    code = "invalid_configuration"


class SarpClientNotOpenError(SarpError):
    """A request was attempted outside the client's managed lifecycle."""

    code = "client_not_open"


class SarpTransportError(SarpError):
    """The request failed before a valid HTTP response was received."""

    code = "transport_error"
    retryable = True

    def __init__(self, method: str, path: str) -> None:
        self.method = method
        self.path = path
        super().__init__("SARP request failed before receiving an HTTP response")


class SarpInvalidResponseError(SarpError):
    """SARP or an intermediary returned a response outside the documented contract."""

    code = "invalid_response"

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.request_id = request_id
        self.retryable = status_code is not None and status_code >= 500
        super().__init__(message)


class SarpApiError(SarpError):
    """A valid SARP error envelope with an unsuccessful HTTP status."""

    code = "api_error"

    def __init__(
        self,
        *,
        status_code: int,
        message: str,
        source: str | None,
        request_id: str | None,
        rate_limit: SarpRateLimit,
    ) -> None:
        self.status_code = status_code
        self.message = message
        self.source = source
        self.request_id = request_id
        self.rate_limit = rate_limit
        self.retryable = status_code == 429 or status_code >= 500
        super().__init__(f"SARP returned HTTP {status_code}: {message or 'unknown_error'}")


class SarpBadRequestError(SarpApiError):
    code = "bad_request"


class SarpAuthenticationError(SarpApiError):
    code = "unauthorized"


class SarpForbiddenError(SarpApiError):
    code = "forbidden"


class SarpNotFoundError(SarpApiError):
    code = "not_found"


class SarpConflictError(SarpApiError):
    code = "conflict"


class SarpRateLimitError(SarpApiError):
    code = "too_many_requests"


class SarpServerError(SarpApiError):
    code = "internal_server_error"


ERROR_TYPES_BY_STATUS: dict[int, type[SarpApiError]] = {
    400: SarpBadRequestError,
    401: SarpAuthenticationError,
    403: SarpForbiddenError,
    404: SarpNotFoundError,
    409: SarpConflictError,
    429: SarpRateLimitError,
    500: SarpServerError,
}


def build_api_error(
    *,
    status_code: int,
    message: str,
    source: str | None,
    request_id: str | None,
    rate_limit: SarpRateLimit,
) -> SarpApiError:
    """Map documented statuses while keeping future server errors recoverable."""

    error_type = ERROR_TYPES_BY_STATUS.get(status_code)
    if error_type is None and status_code >= 500:
        error_type = SarpServerError
    if error_type is None:
        error_type = SarpApiError
    return error_type(
        status_code=status_code,
        message=message,
        source=source,
        request_id=request_id,
        rate_limit=rate_limit,
    )
