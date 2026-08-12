"""Authenticated Railway webhook ingestion."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from arrest_report_agent.config import AppSettings
from arrest_report_agent.config.logging import log_event

from .models import RailwayDeploymentEvent
from .repository import DeploymentRepository

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WebhookAcceptance:
    status: str
    inserted: bool = False


class RailwayWebhookReceiver:
    """Validate event ownership before committing it to the durable inbox."""

    def __init__(
        self,
        settings: AppSettings,
        repository: DeploymentRepository,
        wake_worker: Callable[[], None],
    ) -> None:
        if not settings.railway_webhook_enabled:
            raise ValueError("Railway webhook receiver requires RAILWAY_WEBHOOK_SECRET")
        self.settings = settings
        self.repository = repository
        self._wake_worker = wake_worker

    @property
    def secret(self) -> str:
        value = self.settings.railway_webhook_secret
        if value is None:  # pragma: no cover - constructor guards this invariant.
            raise RuntimeError("Railway webhook secret is unavailable")
        return value.get_secret_value()

    async def receive(self, event: RailwayDeploymentEvent) -> WebhookAcceptance:
        if not event.matches_runtime(
            project_id=self.settings.railway_project_id or "",
            environment_id=self.settings.railway_environment_id or "",
            service_id=self.settings.railway_service_id or "",
        ):
            log_event(
                LOGGER,
                "info",
                "railway_webhook_ignored",
                reason="resource_identity_mismatch",
                event_type=event.event_type.value,
                deployment_id=event.deployment_id,
            )
            return WebhookAcceptance(status="ignored")

        record, inserted = await self.repository.record_event(event)
        if inserted:
            self._wake_worker()
        log_event(
            LOGGER,
            "info",
            "railway_webhook_accepted",
            event_type=record.event_type.value,
            deployment_id=record.deployment_id,
            duplicate=not inserted,
        )
        return WebhookAcceptance(
            status="accepted" if inserted else "duplicate",
            inserted=inserted,
        )
