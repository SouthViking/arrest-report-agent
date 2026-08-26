"""Review-publisher decorator that records structured outputs in PostgreSQL."""

from __future__ import annotations

import logging
from typing import Literal

from arrest_report_agent.application.ports import ReviewPublisher
from arrest_report_agent.application.prompt import PROMPT_VERSION
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.domain import (
    ConversationResponseRoute,
    ManualQuizDelivery,
    ManualQuizPublication,
    ReviewCase,
    ReviewPublication,
)

from .repository import ReportReviewRepository

LOGGER = logging.getLogger(__name__)


class RecordingReviewPublisher:
    """Persist terminal reviews while delegating every Discord side effect."""

    def __init__(
        self,
        delegate: ReviewPublisher,
        repository: ReportReviewRepository,
        case: ReviewCase,
        *,
        model: str,
        reasoning_effort: str,
        persona_id: str,
        persona_revision: int,
        app_version: str,
    ) -> None:
        self.delegate = delegate
        self.repository = repository
        self.case = case
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.persona_id = persona_id
        self.persona_revision = persona_revision
        self.app_version = app_version

    async def mark_review_started(self) -> None:
        await self.delegate.mark_review_started()

    async def publish(self, publication: ReviewPublication) -> None:
        recording = await self.repository.begin_review(
            case=self.case,
            publication=publication,
            report_author_user_id=self._report_author_user_id(),
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            prompt_version=PROMPT_VERSION,
            persona_id=self.persona_id,
            persona_revision=self.persona_revision,
            app_version=self.app_version,
        )
        if not recording.should_publish:
            log_event(
                LOGGER,
                "info",
                "report_review_publication_deduplicated",
                guild_id=self.case.guild_id,
                root_message_id=self.case.root_message_id,
                trigger_message_id=self.case.trigger_message_id,
                review_id=str(recording.id),
                recording_status=recording.status.value,
            )
            return
        try:
            await self.delegate.publish(publication)
        except Exception:
            try:
                await self.repository.mark_failed(recording.id)
            except Exception as persistence_error:
                log_event(
                    LOGGER,
                    "error",
                    "report_review_failure_status_write_failed",
                    exc_info=persistence_error,
                    review_id=str(recording.id),
                    **exception_fields(persistence_error),
                )
            raise
        try:
            await self.repository.mark_published(recording.id)
        except Exception as error:
            # Discord already received the terminal result. Never make the agent publish it twice.
            log_event(
                LOGGER,
                "error",
                "report_review_publish_status_write_failed",
                exc_info=error,
                guild_id=self.case.guild_id,
                root_message_id=self.case.root_message_id,
                trigger_message_id=self.case.trigger_message_id,
                review_id=str(recording.id),
                **exception_fields(error),
            )
            return
        log_event(
            LOGGER,
            "info",
            "report_review_recorded",
            guild_id=self.case.guild_id,
            parent_channel_id=self.case.parent_channel_id,
            root_message_id=self.case.root_message_id,
            trigger_message_id=self.case.trigger_message_id,
            review_id=str(recording.id),
            review_attempt=recording.attempt_number,
            review_scope=publication.review_scope.value,
            verdict=publication.verdict.value,
            finding_count=len(publication.findings),
        )

    async def send_message(
        self,
        content: str,
        response_route: ConversationResponseRoute = ConversationResponseRoute.REPLY,
    ) -> ConversationResponseRoute:
        return await self.delegate.send_message(content, response_route)

    async def mention_roles(
        self,
        role_ids: tuple[int, ...],
        content: str | None = None,
        response_route: ConversationResponseRoute = ConversationResponseRoute.REPLY,
    ) -> ConversationResponseRoute:
        return await self.delegate.mention_roles(role_ids, content, response_route)

    async def set_message_reaction(
        self,
        message_id: int,
        emoji: str,
    ) -> Literal["added", "unchanged", "replaced"]:
        return await self.delegate.set_message_reaction(message_id, emoji)

    async def publish_manual_quiz(
        self,
        publication: ManualQuizPublication,
    ) -> ManualQuizDelivery:
        return await self.delegate.publish_manual_quiz(publication)

    async def publish_failure(self, public_message: str) -> None:
        await self.delegate.publish_failure(public_message)

    def _report_author_user_id(self) -> int | None:
        root = next(
            (message for message in self.case.messages if message.id == self.case.root_message_id),
            None,
        )
        return root.author_id if root is not None else None
