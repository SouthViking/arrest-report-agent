"""Ports implemented by external adapters."""

from __future__ import annotations

from typing import Protocol

from arrest_report_agent.domain import ReviewPublication


class ReviewPublisher(Protocol):
    async def begin(self, case_subject: str | None = None) -> None:
        """Show that the review has started without creating a thread."""

    async def publish(self, publication: ReviewPublication) -> None:
        """Publish a terminal review and execute its Discord side effects."""

    async def reply(self, content: str) -> None:
        """Send a natural terminal response to the triggering Discord message."""

    async def publish_failure(
        self,
        public_message: str,
    ) -> None:
        """Publish a safe contextual failure for manual follow-up."""
