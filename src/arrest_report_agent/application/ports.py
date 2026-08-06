"""Ports implemented by external adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from arrest_report_agent.domain import (
    AgentActivity,
    ConversationResponseRoute,
    ReviewPublication,
)


class ActivityTracker(Protocol):
    async def promote(self, activity: AgentActivity) -> None:
        """Promote one active interaction without downgrading higher-priority work."""


@dataclass(frozen=True)
class GuildMemberProfile:
    """Bounded public member metadata independent from Discord's concrete model."""

    user_id: int
    display_name: str
    username: str
    roles: tuple[str, ...]
    joined_at: datetime | None
    created_at: datetime
    is_bot: bool


@dataclass(frozen=True)
class GuildMemberLookupResult:
    """Partial batch result preserving not-found and transiently unavailable IDs."""

    members: dict[int, GuildMemberProfile]
    not_found: frozenset[int]
    unavailable: frozenset[int]


class GuildMemberDirectory(Protocol):
    async def lookup_members(
        self,
        guild_id: int,
        user_ids: tuple[int, ...],
    ) -> GuildMemberLookupResult:
        """Resolve members only within the deterministic guild supplied by the run."""


@dataclass(frozen=True)
class MessageAttachmentProfile:
    id: int
    filename: str
    url: str
    content_type: str | None
    size: int
    width: int | None
    height: int | None
    input_kind: Literal["image", "file"] | None


@dataclass(frozen=True)
class MessageAttachmentLookupResult:
    """Attachments selected from one permission-checked Discord message."""

    attachments: tuple[MessageAttachmentProfile, ...]
    missing_ids: tuple[int, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class MessageReactionProfile:
    emoji: str
    count: int
    reacted_by_bot: bool


@dataclass(frozen=True)
class GuildMessageProfile:
    message_id: int
    channel_id: int
    author_id: int
    author_display_name: str
    author_username: str
    author_is_bot: bool
    author_roles: tuple[str, ...]
    content: str
    created_at: datetime
    edited_at: datetime | None
    reply_to_message_id: int | None
    embeds: tuple[str, ...]
    attachments: tuple[MessageAttachmentProfile, ...]
    reactions: tuple[MessageReactionProfile, ...]
    truncated: bool = False


@dataclass(frozen=True)
class MessageLookupQuery:
    channel_id: int
    message_id: int | None
    direction: str
    limit: int
    author_ids: tuple[int, ...]
    author_type: str
    has_attachments: bool | None


@dataclass(frozen=True)
class MessageLookupResult:
    messages: tuple[GuildMessageProfile, ...]
    error: str | None = None
    truncated: bool = False


class GuildMessageDirectory(Protocol):
    async def lookup_messages(
        self,
        guild_id: int,
        requester_user_id: int,
        current_channel_id: int,
        query: MessageLookupQuery,
    ) -> MessageLookupResult:
        """Read a bounded message window visible to the requester in the current guild."""

    async def lookup_message_attachments(
        self,
        guild_id: int,
        requester_user_id: int,
        current_channel_id: int,
        channel_id: int,
        message_id: int,
        attachment_ids: tuple[int, ...],
    ) -> MessageAttachmentLookupResult:
        """Resolve selected attachments from one readable message in the current guild."""


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
