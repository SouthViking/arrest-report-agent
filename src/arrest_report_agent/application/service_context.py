"""Local dependency context passed to Agents SDK tools."""

from __future__ import annotations

from dataclasses import dataclass

from arrest_report_agent.domain import ReviewCase, ReviewPublication

from .ports import ReviewPublisher


@dataclass
class ReviewRunContext:
    case: ReviewCase
    publisher: ReviewPublisher
    attempt: int
    started: bool = False
    published: bool = False
    publication: ReviewPublication | None = None

