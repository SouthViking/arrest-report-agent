"""Authenticated ingestion for native GitHub Actions webhooks."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import ValidationError

from arrest_report_agent.config import AppSettings
from arrest_report_agent.config.logging import log_event

from .models import GitHubWebhookPayloadError, GitHubWorkflowRunEvent
from .repository import CiNotificationRepository

LOGGER = logging.getLogger(__name__)
DELIVERY_ID_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,64}$")


class GitHubWebhookAuthenticationError(ValueError):
    """Raised when a GitHub webhook signature is absent or invalid."""


@dataclass(frozen=True, slots=True)
class GitHubWebhookAcceptance:
    status: str
    inserted: bool = False


class GitHubWebhookReceiver:
    """Verify signed workflow_run deliveries and persist failed CI runs."""

    def __init__(
        self,
        settings: AppSettings,
        repository: CiNotificationRepository,
        wake_worker: Callable[[], None],
        *,
        expected_repository: str,
        workflow_name: str = "CI",
    ) -> None:
        if not settings.github_webhook_enabled or settings.github_webhook_secret is None:
            raise ValueError("GitHub webhook receiver requires GITHUB_WEBHOOK_SECRET")
        self._secret = settings.github_webhook_secret.get_secret_value().encode()
        self.repository = repository
        self.wake_worker = wake_worker
        self.expected_repository = expected_repository
        self.workflow_name = workflow_name

    async def receive(
        self,
        raw_body: bytes,
        *,
        event_name: str,
        delivery_id: str,
        signature: str,
    ) -> GitHubWebhookAcceptance:
        self._verify_signature(raw_body, signature)
        if DELIVERY_ID_PATTERN.fullmatch(delivery_id) is None:
            raise GitHubWebhookPayloadError("Invalid GitHub delivery ID")
        if event_name == "ping":
            return GitHubWebhookAcceptance(status="ignored")
        if event_name != "workflow_run":
            return GitHubWebhookAcceptance(status="ignored")
        try:
            payload = json.loads(raw_body)
            event = GitHubWorkflowRunEvent.model_validate(payload)
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as error:
            raise GitHubWebhookPayloadError("Invalid GitHub workflow payload") from error
        if not event.matches_repository(self.expected_repository):
            log_event(
                LOGGER,
                "warning",
                "github_webhook_repository_rejected",
                delivery_id=delivery_id,
                repository=event.repository.full_name,
            )
            return GitHubWebhookAcceptance(status="ignored")
        run = event.workflow_run
        if (
            event.action != "completed"
            or run.status != "completed"
            or run.conclusion != "failure"
        ):
            return GitHubWebhookAcceptance(status="ignored")
        if run.name != self.workflow_name:
            return GitHubWebhookAcceptance(status="ignored")
        _, inserted = await self.repository.record_event(delivery_id, event)
        if inserted:
            self.wake_worker()
        log_event(
            LOGGER,
            "info",
            "github_workflow_webhook_accepted",
            delivery_id=delivery_id,
            repository=event.repository.full_name,
            workflow=event.workflow_run.name,
            run_id=event.workflow_run.id,
            run_attempt=event.workflow_run.run_attempt,
            inserted=inserted,
        )
        return GitHubWebhookAcceptance(
            status="accepted" if inserted else "duplicate",
            inserted=inserted,
        )

    def _verify_signature(self, raw_body: bytes, signature: str) -> None:
        expected = "sha256=" + hmac.new(
            self._secret,
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        if not signature or not hmac.compare_digest(expected, signature):
            raise GitHubWebhookAuthenticationError("Invalid GitHub webhook signature")
