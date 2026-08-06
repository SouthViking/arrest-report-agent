"""Local dependency context passed to Agents SDK tools."""

from __future__ import annotations

from dataclasses import dataclass, field

from agents.usage import Usage

from arrest_report_agent.domain import (
    ConversationResponseRoute,
    RequestIntent,
    ReviewCase,
    ReviewPublication,
)

from .ports import ActivityTracker, ReviewPublisher


@dataclass
class ReviewRunContext:
    case: ReviewCase
    publisher: ReviewPublisher
    attempt: int
    activity: ActivityTracker | None = None
    request_intent: RequestIntent | None = None
    has_revisable_material: bool = False
    agent_started: bool = False
    agent_completed: bool = False
    review_marked: bool = False
    published: bool = False
    message_sent: bool = False
    messages_sent_count: int = 0
    selected_response_route: ConversationResponseRoute | None = None
    interaction_completed: bool = False
    publication: ReviewPublication | None = None
    guardrail_usage: Usage = field(default_factory=Usage)
