"""Local dependency context passed to Agents SDK tools."""

from __future__ import annotations

from dataclasses import dataclass, field

from agents.usage import Usage

from arrest_report_agent.domain import ReviewCase, ReviewPublication

from .ports import ReviewPublisher


@dataclass
class ReviewRunContext:
    case: ReviewCase
    publisher: ReviewPublisher
    attempt: int
    started: bool = False
    published: bool = False
    replied: bool = False
    publication: ReviewPublication | None = None
    guardrail_usage: Usage = field(default_factory=Usage)
