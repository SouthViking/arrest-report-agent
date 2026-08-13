"""Deterministic GitHub Actions failure notification subsystem."""

from .discord import CiNotificationWorker, build_ci_failure_embed
from .models import GitHubWebhookPayloadError, GitHubWorkflowRunEvent
from .repository import CiNotificationRepository
from .webhook import (
    GitHubWebhookAcceptance,
    GitHubWebhookAuthenticationError,
    GitHubWebhookReceiver,
)

__all__ = [
    "CiNotificationRepository",
    "CiNotificationWorker",
    "GitHubWebhookAcceptance",
    "GitHubWebhookAuthenticationError",
    "GitHubWebhookPayloadError",
    "GitHubWebhookReceiver",
    "GitHubWorkflowRunEvent",
    "build_ci_failure_embed",
]
