"""Local dependency context passed to Agents SDK tools."""

from __future__ import annotations

from dataclasses import dataclass, field

from agents.usage import Usage

from arrest_report_agent.domain import (
    AgentAccessLevel,
    ConversationResponseRoute,
    RequestIntent,
    ReviewCase,
    ReviewPublication,
)

from .ports import (
    ActivityTracker,
    GuildMemberDirectory,
    GuildMessageDirectory,
    ReviewPublisher,
)


@dataclass
class ReviewRunContext:
    case: ReviewCase
    publisher: ReviewPublisher
    attempt: int
    access_level: AgentAccessLevel = AgentAccessLevel.FULL
    owner_round: int | None = None
    owner_max_rounds: int | None = None
    owner_reservation_result: str | None = None
    activity: ActivityTracker | None = None
    member_directory: GuildMemberDirectory | None = None
    message_directory: GuildMessageDirectory | None = None
    request_intent: RequestIntent | None = None
    has_revisable_material: bool = False
    agent_started: bool = False
    agent_completed: bool = False
    review_marked: bool = False
    published: bool = False
    message_sent: bool = False
    messages_sent_count: int = 0
    reactions_updated_count: int = 0
    selected_response_route: ConversationResponseRoute | None = None
    interaction_completed: bool = False
    publication: ReviewPublication | None = None
    guardrail_usage: Usage = field(default_factory=Usage)
    loaded_attachment_ids: set[int] = field(default_factory=set)
    loaded_image_count: int = 0
    loaded_file_count: int = 0
    loaded_file_bytes: int = 0

    def __post_init__(self) -> None:
        """Seed multimodal budgets with attachments already present in the runner input."""

        if self.loaded_attachment_ids:
            return
        self.loaded_attachment_ids = {attachment.id for attachment in self.case.attachments}
        self.loaded_image_count = sum(1 for attachment in self.case.attachments if attachment.is_image)
        self.loaded_file_count = sum(1 for attachment in self.case.attachments if not attachment.is_image)
        self.loaded_file_bytes = sum(
            attachment.size for attachment in self.case.attachments if not attachment.is_image
        )
