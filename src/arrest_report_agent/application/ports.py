"""Ports implemented by external adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Protocol
from uuid import UUID

from arrest_report_agent.domain import (
    AgentActivity,
    ConversationResponseRoute,
    ManualQuizDelivery,
    ManualQuizPublication,
    ReviewPublication,
)


class ActivityTracker(Protocol):
    async def transition(self, activity: AgentActivity) -> None:
        """Move one active interaction to its current user-facing phase."""


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
    role_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class GuildMemberLookupResult:
    """Partial batch result preserving not-found and transiently unavailable IDs."""

    members: dict[int, GuildMemberProfile]
    not_found: frozenset[int]
    unavailable: frozenset[int]


@dataclass(frozen=True)
class GuildRoleProfile:
    """Compact public role metadata used for member discovery."""

    role_id: int
    name: str
    position: int
    managed: bool
    member_count: int


@dataclass(frozen=True)
class GuildRoleSearchQuery:
    query: str | None
    role_ids: tuple[int, ...]
    include_managed: bool
    sort_by: str
    sort_direction: str
    limit: int
    cursor: str | None
    include_member_count: bool = True


@dataclass(frozen=True)
class GuildRoleSearchResult:
    roles: tuple[GuildRoleProfile, ...]
    next_cursor: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class GuildMemberSearchQuery:
    query: str | None
    user_ids: tuple[int, ...]
    role_ids_any: tuple[int, ...]
    role_ids_all: tuple[int, ...]
    role_ids_none: tuple[int, ...]
    is_bot: bool | None
    joined_after: datetime | None
    joined_before: datetime | None
    sort_by: str
    sort_direction: str
    limit: int
    cursor: str | None


type GuildMemberNameMatchStrategy = Literal[
    "exact_terms",
    "initial_and_surname",
    "surname",
]


@dataclass(frozen=True)
class GuildMemberSearchResult:
    members: tuple[GuildMemberProfile, ...]
    next_cursor: str | None = None
    error: str | None = None
    name_match_strategy: GuildMemberNameMatchStrategy | None = None
    candidate_count: int | None = None


@dataclass(frozen=True)
class ChannelMemberSearchQuery:
    channel_id: int
    membership: str
    message_limit: int
    members: GuildMemberSearchQuery


class GuildMemberDirectory(Protocol):
    async def lookup_members(
        self,
        guild_id: int,
        user_ids: tuple[int, ...],
    ) -> GuildMemberLookupResult:
        """Resolve members only within the deterministic guild supplied by the run."""

    async def search_roles(
        self,
        guild_id: int,
        query: GuildRoleSearchQuery,
    ) -> GuildRoleSearchResult:
        """Search roles only inside the deterministic guild supplied by the run."""

    async def search_members(
        self,
        guild_id: int,
        query: GuildMemberSearchQuery,
    ) -> GuildMemberSearchResult:
        """Search members only inside the deterministic guild supplied by the run."""

    async def search_channel_members(
        self,
        guild_id: int,
        requester_user_id: int,
        current_channel_id: int,
        query: ChannelMemberSearchQuery,
    ) -> GuildMemberSearchResult:
        """Search a permission-checked channel audience inside the deterministic guild."""


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


@dataclass(frozen=True)
class ArrestReportSearchQuery:
    suspect_name: str | None
    officer_name: str | None
    date_from: date | None
    date_to: date | None
    page: int
    limit: int
    sort_field: str | None
    sort_order: Literal["asc", "desc"]
    exact_suspect: bool = False
    exact_officer: bool = False


@dataclass(frozen=True)
class ArrestReportSummary:
    report_id: int
    suspect_name: str
    officer_name: str
    description: str
    created_at: datetime


@dataclass(frozen=True)
class ArrestReportSearchResult:
    reports: tuple[ArrestReportSummary, ...]
    total: int
    page: int
    limit: int
    has_more: bool
    error: str | None = None
    retry_after_seconds: float | None = None


@dataclass(frozen=True)
class ArrestReportCharge:
    charge_id: int
    code: str | None
    name: str
    charge_type: str | None
    hours: int | float | None
    fine: int | float | None


@dataclass(frozen=True)
class ArrestReportDetail:
    report_id: int
    suspect_name: str
    officer_name: str
    description: str
    created_at: datetime
    updated_at: datetime | None
    charges: tuple[ArrestReportCharge, ...]


@dataclass(frozen=True)
class ArrestReportDetailResult:
    report: ArrestReportDetail | None
    error: str | None = None
    retry_after_seconds: float | None = None


class ArrestReportLookup(Protocol):
    async def search(self, query: ArrestReportSearchQuery) -> ArrestReportSearchResult:
        """Search compact reports within the deterministically configured faction."""

    async def get(self, report_id: int) -> ArrestReportDetailResult:
        """Fetch one current arrest report by its SARP identifier."""

    async def bind(
        self,
        *,
        guild_id: int,
        root_message_id: int,
        parent_channel_id: int,
        thread_id: int,
        report_id: int,
        actor_user_id: int,
        trigger_message_id: int,
    ) -> str | None:
        """Replace the report reference, returning a safe error code on failure."""

    async def get_current(
        self,
        *,
        guild_id: int,
        root_message_id: int,
    ) -> ArrestReportDetailResult:
        """Fetch the current SARP detail linked to one Discord report root."""


type ReportImageCategory = Literal["suspect_photo", "supporting_image"]


@dataclass(frozen=True)
class ReportImageAsset:
    asset_id: str
    filename: str
    content_type: str
    size_bytes: int
    position: int
    payload: bytes


@dataclass(frozen=True)
class ReportImageResult:
    report_id: int | None
    owner_user_id: int | None
    images: tuple[ReportImageAsset, ...]
    error: str | None = None


class ReportImageLookup(Protocol):
    async def get_images(
        self,
        *,
        guild_id: int,
        submission_id: UUID | None,
        root_message_id: int | None,
        report_id: int | None,
        category: ReportImageCategory,
    ) -> ReportImageResult:
        """Load one bounded category of structured report images."""


type CharacterLegalFactCheck = Literal[
    "prior_serious_convictions",
    "overdue_unpaid_fines",
]
type CharacterLegalFactStatus = Literal["confirmed", "incomplete", "unavailable"]


@dataclass(frozen=True)
class CharacterLegalFactsQuery:
    character_name: str
    checks: tuple[CharacterLegalFactCheck, ...]
    reference_at: datetime
    exclude_arrest_id: int | None
    max_matches_per_check: int


@dataclass(frozen=True)
class PriorSeriousConvictionMatch:
    arrest_id: int
    arrested_at: datetime
    charge_code: str | None
    charge_name: str
    charge_type: str


@dataclass(frozen=True)
class OverdueUnpaidFineMatch:
    fine_id: int
    description: str
    amount: int | float
    issued_at: datetime
    overdue_since: datetime
    overdue_days_at_reference: int


@dataclass(frozen=True)
class CharacterLegalFactResult[LegalFactMatchT]:
    status: CharacterLegalFactStatus
    value: bool | None
    matches: tuple[LegalFactMatchT, ...]
    match_count: int
    matches_truncated: bool
    records_scanned: int
    pages_scanned: int
    error: str | None = None
    retry_after_seconds: float | None = None


@dataclass(frozen=True)
class CharacterLegalFactsResult:
    character_name: str | None
    reference_at: datetime
    prior_serious_convictions: CharacterLegalFactResult[PriorSeriousConvictionMatch] | None
    overdue_unpaid_fines: CharacterLegalFactResult[OverdueUnpaidFineMatch] | None
    error: str | None = None
    retry_after_seconds: float | None = None


class CharacterLegalFactsLookup(Protocol):
    async def check(self, query: CharacterLegalFactsQuery) -> CharacterLegalFactsResult:
        """Resolve one exact character and deterministically compute requested legal facts."""


@dataclass(frozen=True)
class EvidenceBagItem:
    """One analyzed item preserved as authoritative SARP text."""

    analysis_id: int
    sample_id: int
    result: str
    officer_name: str
    agency: str | None
    examined_at: datetime


@dataclass(frozen=True)
class EvidenceBagCustodyEvent:
    """One normalized chain-of-custody event."""

    event_id: int
    action: str
    officer_name: str
    created_at: datetime


@dataclass(frozen=True)
class EvidenceBagDetail:
    """Current analyzed contents and custody history for one evidence bag."""

    evidence_id: int
    items: tuple[EvidenceBagItem, ...]
    custody: tuple[EvidenceBagCustodyEvent, ...]


@dataclass(frozen=True)
class EvidenceBagLookupResult:
    detail: EvidenceBagDetail | None
    error: str | None = None
    retry_after_seconds: float | None = None


class EvidenceBagLookup(Protocol):
    async def get(self, evidence_id: int) -> EvidenceBagLookupResult:
        """Fetch one evidence bag while translating provider failures to safe states."""


@dataclass(frozen=True)
class FactionFrequencyProfile:
    """One configured frequency projected without SARP technical identifiers."""

    frequency: int
    name: str
    division_name: str | None


@dataclass(frozen=True)
class FactionFrequencyLookupResult:
    frequencies: tuple[FactionFrequencyProfile, ...]
    division_catalog_complete: bool = True
    error: str | None = None
    retry_after_seconds: float | None = None


class FactionFrequencyLookup(Protocol):
    async def list(self) -> FactionFrequencyLookupResult:
        """List frequencies only for the faction bound by the active guild configuration."""


FactionOfficerRankStatus = Literal[
    "matched",
    "possible_match",
    "ambiguous",
    "not_found",
    "invalid_name",
    "rank_unknown",
    "unavailable",
]


@dataclass(frozen=True)
class FactionOfficerRankLookupResult:
    """Current faction rank for one report officer, with explicit identity uncertainty."""

    status: FactionOfficerRankStatus
    officer_name: str
    member_name: str | None = None
    current_rank: str | None = None
    match_method: Literal["normalized_exact", "compact_exact", "fuzzy"] | None = None
    candidates: tuple[str, ...] = ()
    error: str | None = None
    retry_after_seconds: float | None = None


class FactionOfficerRankLookup(Protocol):
    async def find(self, officer_name: str, *, fuzzy: bool = True) -> FactionOfficerRankLookupResult:
        """Resolve only the current configured faction roster, never a historical rank."""


@dataclass(frozen=True)
class PollAnswerDraft:
    """One validated native Discord poll answer."""

    text: str
    emoji: str | None = None


@dataclass(frozen=True)
class PollCreateRequest:
    """Flexible native-poll configuration selected by the agent."""

    question: str
    answers: tuple[PollAnswerDraft, ...]
    duration_hours: int
    allow_multiselect: bool
    silent: bool
    intro: str | None = None


@dataclass(frozen=True)
class PollListQuery:
    """Metadata-only filters for polls previously created by the bot."""

    status: Literal["any", "open", "ended", "deleted"]
    creator_user_ids: tuple[int, ...]
    only_created_by_requester: bool
    channel_ids: tuple[int, ...]
    created_after: datetime | None
    created_before: datetime | None
    expires_after: datetime | None
    expires_before: datetime | None
    sort_direction: Literal["asc", "desc"]
    page: int
    limit: int


class PollManager(Protocol):
    """Create and inspect native Discord polls within one deterministic guild."""

    async def create(
        self,
        *,
        guild_id: int,
        current_channel_id: int,
        requester_user_id: int,
        target_channel_id: int | None,
        request: PollCreateRequest,
    ) -> dict[str, object]:
        """Publish one native poll and persist only its lookup metadata."""

    async def list(
        self,
        *,
        guild_id: int,
        requester_user_id: int,
        query: PollListQuery,
    ) -> dict[str, object]:
        """List compact metadata for bot-created polls visible to the requester."""

    async def get(
        self,
        *,
        guild_id: int,
        requester_user_id: int,
        message_id: int,
        include_voters: bool,
        answer_ids: tuple[int, ...],
        voter_limit_per_answer: int,
        after_user_id: int | None,
    ) -> dict[str, object]:
        """Fetch current poll content and optional voter pages directly from Discord."""

    async def end(
        self,
        *,
        guild_id: int,
        requester_user_id: int,
        message_id: int,
    ) -> dict[str, object]:
        """End one poll created and indexed by this bot."""


class ReviewPublisher(Protocol):
    async def mark_review_started(self) -> None:
        """Show deterministically that a classified review has started."""

    async def publish(self, publication: ReviewPublication) -> None:
        """Publish a structured review and execute its Discord side effects."""

    async def send_message(
        self,
        content: str,
        response_route: ConversationResponseRoute = ConversationResponseRoute.REPLY,
    ) -> ConversationResponseRoute:
        """Send a message through a safe route and return the effective route."""

    async def mention_roles(
        self,
        role_ids: tuple[int, ...],
        content: str | None = None,
        response_route: ConversationResponseRoute = ConversationResponseRoute.REPLY,
    ) -> ConversationResponseRoute:
        """Notify explicitly selected roles from the active guild through a safe route."""

    async def set_message_reaction(
        self,
        message_id: int,
        emoji: str,
    ) -> Literal["added", "unchanged", "replaced"]:
        """Set one bot reaction on a message in the active Discord conversation."""

    async def publish_manual_quiz(
        self,
        publication: ManualQuizPublication,
    ) -> ManualQuizDelivery:
        """Publish a transient interactive quiz through the current Discord conversation."""

    async def publish_failure(
        self,
        public_message: str,
    ) -> None:
        """Publish a safe contextual failure for manual follow-up."""
