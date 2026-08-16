"""Shared SARP API transport infrastructure."""

from .client import DEFAULT_BASE_URL, SarpClient, SarpClientConfig
from .errors import (
    SarpApiError,
    SarpAuthenticationError,
    SarpBadRequestError,
    SarpClientNotOpenError,
    SarpConfigurationError,
    SarpConflictError,
    SarpError,
    SarpForbiddenError,
    SarpInvalidResponseError,
    SarpNotFoundError,
    SarpRateLimitError,
    SarpServerError,
    SarpTransportError,
)
from .models import (
    SarpEnvelope,
    SarpPenalCodeCategory,
    SarpPenalCodeCharge,
    SarpPenalCodePayload,
    SarpRateLimit,
    SarpResponse,
)
from .penal_code import SarpPenalCodeProvider, build_charge_catalog

__all__ = [
    "DEFAULT_BASE_URL",
    "SarpApiError",
    "SarpAuthenticationError",
    "SarpBadRequestError",
    "SarpClient",
    "SarpClientConfig",
    "SarpClientNotOpenError",
    "SarpConfigurationError",
    "SarpConflictError",
    "SarpEnvelope",
    "SarpError",
    "SarpForbiddenError",
    "SarpInvalidResponseError",
    "SarpNotFoundError",
    "SarpPenalCodeCategory",
    "SarpPenalCodeCharge",
    "SarpPenalCodePayload",
    "SarpPenalCodeProvider",
    "SarpRateLimit",
    "SarpRateLimitError",
    "SarpResponse",
    "SarpServerError",
    "SarpTransportError",
    "build_charge_catalog",
]
