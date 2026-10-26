"""Local dependency context passed to Agents SDK tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from arrest_report_agent.domain import (
    AgentAccessLevel,
    ConversationResponseRoute,
    ReviewCase,
    ReviewPublication,
    ReviewVerdict,
)
from arrest_report_agent.knowledge import ChargeCatalogSnapshot

from .approvals import StaffApproval
from .conversations import LiveConversation
from .ports import (
    ActivityTracker,
    ArrestReportDetail,
    GuildMemberDirectory,
    GuildMessageDirectory,
    PollManager,
    ReviewPublisher,
)

ReportSnapshotRefresher = Callable[
    [ArrestReportDetail | None, ReviewVerdict],
    Awaitable[bool],
]


@dataclass
class ReviewRunContext:
    case: ReviewCase
    publisher: ReviewPublisher
    attempt: int
    live_conversation: LiveConversation | None = None
    access_level: AgentAccessLevel = AgentAccessLevel.FULL
    owner_round: int | None = None
    owner_max_rounds: int | None = None
    owner_reservation_result: str | None = None
    charge_catalog_snapshot: ChargeCatalogSnapshot | None = None
    activity: ActivityTracker | None = None
    member_directory: GuildMemberDirectory | None = None
    message_directory: GuildMessageDirectory | None = None
    poll_manager: PollManager | None = None
    review_marked: bool = False
    published: bool = False
    completed: bool = False
    messages_sent_count: int = 0
    reactions_updated_count: int = 0
    selected_response_route: ConversationResponseRoute | None = None
    publication: ReviewPublication | None = None
    tool_argument_recovery_counts: dict[str, int] = field(default_factory=dict)
    tool_argument_recovery_exhausted: bool = False
    started_tool_call_ids: set[str] = field(default_factory=set)
    loaded_attachment_ids: set[int] = field(default_factory=set)
    loaded_report_asset_ids: set[str] = field(default_factory=set)
    preloaded_arrest_report: ArrestReportDetail | None = None
    latest_arrest_report: ArrestReportDetail | None = None
    arrest_warrant_context: str | None = None
    report_snapshot_refresher: ReportSnapshotRefresher | None = None
    report_snapshot_current: bool = False
    staff_approval_authorizer: Callable[[int], Awaitable[StaffApproval | None]] | None = None
    loaded_image_count: int = 0
    loaded_historical_image_count: int = 0
    loaded_file_count: int = 0
    loaded_file_bytes: int = 0
    arrest_search_calls: int = 0
    arrest_detail_calls: int = 0
    arrest_warrant_calls: int = 0
    arrest_candidate_report_ids: set[int] = field(default_factory=set)
    character_legal_fact_calls: int = 0
    evidence_bag_calls: int = 0
    evidence_bag_ids_queried: int = 0
    faction_frequency_calls: int = 0
    faction_officer_rank_calls: int = 0
    manual_list_calls: int = 0
    manual_search_calls: int = 0
    manual_read_calls: int = 0
    poll_create_calls: int = 0
    poll_list_calls: int = 0
    poll_detail_calls: int = 0
    poll_end_calls: int = 0

    def __post_init__(self) -> None:
        """Seed multimodal budgets with attachments already present in the runner input."""

        if self.latest_arrest_report is None:
            self.latest_arrest_report = self.preloaded_arrest_report
        if self.loaded_attachment_ids:
            return
        self.loaded_attachment_ids = {
            attachment.id for attachment in self.case.attachments if not attachment.is_image
        }
        self.loaded_image_count = 0
        self.loaded_file_count = sum(1 for attachment in self.case.attachments if not attachment.is_image)
        self.loaded_file_bytes = sum(
            attachment.size for attachment in self.case.attachments if not attachment.is_image
        )
