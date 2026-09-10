"""Provider-neutral voluntary contribution ingestion."""

from .buy_me_a_coffee import (
    BuyMeACoffeeWebhookAcceptance,
    BuyMeACoffeeWebhookAuthenticationError,
    BuyMeACoffeeWebhookPayloadError,
    BuyMeACoffeeWebhookReceiver,
)
from .exchange_rates import ExchangeRateError, FrankfurterExchangeRateClient
from .models import (
    ContributionEvent,
    ContributionEventKind,
    ContributionNormalizationStatus,
    ContributionStatus,
    ContributionToNormalize,
    ExchangeRate,
    SupporterProfile,
    SupporterRanking,
)
from .repository import ContributionIngestResult, SupportContributionRepository
from .worker import ContributionNormalizationWorker

__all__ = [
    "BuyMeACoffeeWebhookAcceptance",
    "BuyMeACoffeeWebhookAuthenticationError",
    "BuyMeACoffeeWebhookPayloadError",
    "BuyMeACoffeeWebhookReceiver",
    "ContributionEvent",
    "ContributionEventKind",
    "ContributionIngestResult",
    "ContributionNormalizationStatus",
    "ContributionNormalizationWorker",
    "ContributionStatus",
    "ContributionToNormalize",
    "ExchangeRate",
    "ExchangeRateError",
    "FrankfurterExchangeRateClient",
    "SupportContributionRepository",
    "SupporterProfile",
    "SupporterRanking",
]
