"""Ports implemented by external adapters."""

from __future__ import annotations

from typing import Protocol

from arrest_report_agent.domain import (
    AgentActivity,
    ConversationResponseRoute,
    ReviewPublication,
)


class ActivityTracker(Protocol):
    async def promote(self, activity: AgentActivity) -> None:
        """Promote one active interaction without downgrading higher-priority work."""


class ReviewPublisher(Protocol):
    async def mark_review_started(self) -> None:
        """Show deterministically that a classified review has started."""

    async def publish(self, publication: ReviewPublication) -> None:
        """Publish a terminal review and execute its Discord side effects."""

    async def send_message(
        self,
        content: str,
        response_route: ConversationResponseRoute = ConversationResponseRoute.REPLY,
    ) -> ConversationResponseRoute:
        """Send a message through a safe route and return the effective route."""

    async def publish_failure(
        self,
        public_message: str,
    ) -> None:
        """Publish a safe contextual failure for manual follow-up."""
