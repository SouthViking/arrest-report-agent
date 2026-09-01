from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from agents import ToolOutputFileContent, ToolOutputImage, ToolOutputText
from agents.tool_context import ToolContext

from arrest_report_agent.application.ports import (
    ChannelMemberSearchQuery,
    GuildMemberLookupResult,
    GuildMemberNameMatchStrategy,
    GuildMemberProfile,
    GuildMemberSearchQuery,
    GuildMemberSearchResult,
    GuildMessageProfile,
    GuildRoleProfile,
    GuildRoleSearchQuery,
    GuildRoleSearchResult,
    MessageAttachmentLookupResult,
    MessageAttachmentProfile,
    MessageLookupQuery,
    MessageLookupResult,
    MessageReactionProfile,
    PollCreateRequest,
    PollListQuery,
)
from arrest_report_agent.application.service_context import ReviewRunContext
from arrest_report_agent.application.tools import _member_search_output, build_agent_tools
from arrest_report_agent.domain import (
    AgentAccessLevel,
    AgentActivity,
    ConversationResponseRoute,
    DiscordAttachment,
    InteractionMode,
    ManualQuizDelivery,
    ManualQuizPublication,
    QuizAnswerMode,
    QuizCategory,
    QuizDifficulty,
    QuizDurationSeconds,
    RequestIntent,
    ReviewCase,
    ReviewVerdict,
)
from arrest_report_agent.knowledge import (
    ChargeCatalog,
    ChargeCatalogSnapshot,
    ChargeCatalogSource,
    ReportGuide,
)
from arrest_report_agent.knowledge.catalog import Charge, ChargeCategory


def _catalog(identifier: int, title: str) -> ChargeCatalog:
    return ChargeCatalog(
        [
            ChargeCategory(
                id=identifier,
                title=title,
                charges=[
                    Charge(
                        id=identifier,
                        category=identifier,
                        code=f"TEST-{identifier}",
                        charge=title,
                        type="Test",
                        description=title,
                    )
                ],
            )
        ],
        f"hash-{identifier}",
    )


class MutableCatalogProvider:
    def __init__(self, snapshot: ChargeCatalogSnapshot) -> None:
        self.current = snapshot

    def snapshot(self) -> ChargeCatalogSnapshot:
        return self.current


class FakePublisher:
    def __init__(self) -> None:
        self.markers = 0
        self.publications = []
        self.messages: list[str] = []
        self.routes: list[ConversationResponseRoute] = []
        self.reactions: list[tuple[int, str]] = []
        self.role_mentions: list[tuple[tuple[int, ...], str | None]] = []
        self.manual_quizzes: list[ManualQuizPublication] = []
        self.manual_quiz_delivery = ManualQuizDelivery(
            ok=True,
            status="published",
            question_id="dynamic-question-id",
        )

    async def mark_review_started(self) -> None:
        self.markers += 1

    async def publish(self, publication) -> None:
        self.publications.append(publication)

    async def send_message(
        self,
        content: str,
        response_route: ConversationResponseRoute = ConversationResponseRoute.REPLY,
    ) -> ConversationResponseRoute:
        self.messages.append(content)
        self.routes.append(response_route)
        return response_route

    async def mention_roles(
        self,
        role_ids: tuple[int, ...],
        content: str | None = None,
        response_route: ConversationResponseRoute = ConversationResponseRoute.REPLY,
    ) -> ConversationResponseRoute:
        self.role_mentions.append((role_ids, content))
        self.routes.append(response_route)
        return response_route

    async def set_message_reaction(self, message_id: int, emoji: str) -> str:
        self.reactions.append((message_id, emoji))
        return "replaced"

    async def publish_manual_quiz(
        self,
        publication: ManualQuizPublication,
    ) -> ManualQuizDelivery:
        self.manual_quizzes.append(publication)
        return self.manual_quiz_delivery

    async def publish_failure(self, public_message) -> None:
        raise AssertionError("not expected")


@pytest.mark.asyncio
async def test_report_owner_message_tool_cannot_leave_active_report() -> None:
    guild_id = 98765432109876543
    parent_channel_id = 11111111111111111
    thread_id = 22222222222222222
    root_message_id = 33333333333333333
    requester_id = 44444444444444444
    directory = FakeMessageDirectory(MessageLookupResult(messages=()))
    run_context = ReviewRunContext(
        case=ReviewCase(
            guild_id=guild_id,
            parent_channel_id=parent_channel_id,
            channel_id=thread_id,
            thread_id=thread_id,
            root_message_id=root_message_id,
            trigger_message_id=55555555555555555,
            requester_user_id=requester_id,
            interaction_mode=InteractionMode.THREAD_FOLLOW_UP,
            messages=[],
            attachments=[],
        ),
        publisher=FakePublisher(),
        attempt=1,
        access_level=AgentAccessLevel.REPORT_OWNER,
        message_directory=directory,
    )
    tools = {
        tool.name: tool
        for tool in build_agent_tools(
            ChargeCatalog.load_default(),
            ReportGuide.load_default(),
            access_level=AgentAccessLevel.REPORT_OWNER,
        )
    }
    outside_arguments = json.dumps(
        {
            "channel_id": "66666666666666666",
            "message_id": str(root_message_id),
            "direction": "exact",
            "limit": 1,
        }
    )
    outside = await tools["get_messages"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="get_messages",
            tool_call_id="owner-outside",
            tool_arguments=outside_arguments,
        ),
        outside_arguments,
    )
    parent_latest_arguments = json.dumps(
        {
            "channel_id": str(parent_channel_id),
            "message_id": None,
            "direction": "latest",
            "limit": 10,
        }
    )
    parent_latest = await tools["get_messages"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="get_messages",
            tool_call_id="owner-parent-latest",
            tool_arguments=parent_latest_arguments,
        ),
        parent_latest_arguments,
    )

    assert outside["error"] == "message_outside_active_report"
    assert parent_latest["error"] == "message_outside_active_report"
    assert directory.calls == []


class ActivityRecorder:
    def __init__(self) -> None:
        self.activities: list[AgentActivity] = []

    async def transition(self, activity: AgentActivity) -> None:
        self.activities.append(activity)


class FakePollManager:
    def __init__(self) -> None:
        self.created: list[tuple[int, int, int, int | None, PollCreateRequest]] = []
        self.listed: list[tuple[int, int, PollListQuery]] = []
        self.read: list[tuple[int, int, int, bool, tuple[int, ...], int, int | None]] = []
        self.ended: list[tuple[int, int, int]] = []

    async def create(
        self,
        *,
        guild_id: int,
        current_channel_id: int,
        requester_user_id: int,
        target_channel_id: int | None,
        request: PollCreateRequest,
    ) -> dict[str, object]:
        self.created.append(
            (guild_id, current_channel_id, requester_user_id, target_channel_id, request)
        )
        return {"ok": True, "message_id": "66666666666666666", "interaction_complete": False}

    async def list(
        self,
        *,
        guild_id: int,
        requester_user_id: int,
        query: PollListQuery,
    ) -> dict[str, object]:
        self.listed.append((guild_id, requester_user_id, query))
        return {"ok": True, "polls": []}

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
        self.read.append(
            (
                guild_id,
                requester_user_id,
                message_id,
                include_voters,
                answer_ids,
                voter_limit_per_answer,
                after_user_id,
            )
        )
        return {"ok": True, "poll": {"message_id": str(message_id)}}

    async def end(
        self,
        *,
        guild_id: int,
        requester_user_id: int,
        message_id: int,
    ) -> dict[str, object]:
        self.ended.append((guild_id, requester_user_id, message_id))
        return {"ok": True, "message_id": str(message_id), "interaction_complete": False}


class FakeMemberDirectory:
    def __init__(self, result: GuildMemberLookupResult) -> None:
        self.result = result
        self.calls: list[tuple[int, tuple[int, ...]]] = []

    async def lookup_members(
        self,
        guild_id: int,
        user_ids: tuple[int, ...],
    ) -> GuildMemberLookupResult:
        self.calls.append((guild_id, user_ids))
        return self.result


class FakeDiscoveryDirectory(FakeMemberDirectory):
    def __init__(
        self,
        *,
        role_result: GuildRoleSearchResult | None = None,
        member_result: GuildMemberSearchResult | None = None,
    ) -> None:
        super().__init__(
            GuildMemberLookupResult(
                members={},
                not_found=frozenset(),
                unavailable=frozenset(),
            )
        )
        self.role_result = role_result or GuildRoleSearchResult(roles=())
        self.member_result = member_result or GuildMemberSearchResult(members=())
        self.role_calls: list[tuple[int, GuildRoleSearchQuery]] = []
        self.member_calls: list[tuple[int, GuildMemberSearchQuery]] = []
        self.channel_member_calls: list[tuple[int, int, int, ChannelMemberSearchQuery]] = []

    async def search_roles(
        self,
        guild_id: int,
        query: GuildRoleSearchQuery,
    ) -> GuildRoleSearchResult:
        self.role_calls.append((guild_id, query))
        return self.role_result

    async def search_members(
        self,
        guild_id: int,
        query: GuildMemberSearchQuery,
    ) -> GuildMemberSearchResult:
        self.member_calls.append((guild_id, query))
        return self.member_result

    async def search_channel_members(
        self,
        guild_id: int,
        requester_user_id: int,
        current_channel_id: int,
        query: ChannelMemberSearchQuery,
    ) -> GuildMemberSearchResult:
        self.channel_member_calls.append((guild_id, requester_user_id, current_channel_id, query))
        return self.member_result


class FakeMessageDirectory:
    def __init__(
        self,
        result: MessageLookupResult,
        attachment_result: MessageAttachmentLookupResult | None = None,
    ) -> None:
        self.result = result
        self.attachment_result = attachment_result or MessageAttachmentLookupResult(attachments=())
        self.calls: list[tuple[int, int, int, MessageLookupQuery]] = []
        self.attachment_calls: list[tuple[int, int, int, int, int, tuple[int, ...]]] = []

    async def lookup_messages(
        self,
        guild_id: int,
        requester_user_id: int,
        current_channel_id: int,
        query: MessageLookupQuery,
    ) -> MessageLookupResult:
        self.calls.append((guild_id, requester_user_id, current_channel_id, query))
        return self.result

    async def lookup_message_attachments(
        self,
        guild_id: int,
        requester_user_id: int,
        current_channel_id: int,
        channel_id: int,
        message_id: int,
        attachment_ids: tuple[int, ...],
    ) -> MessageAttachmentLookupResult:
        self.attachment_calls.append(
            (
                guild_id,
                requester_user_id,
                current_channel_id,
                channel_id,
                message_id,
                attachment_ids,
            )
        )
        return self.attachment_result


def test_conversation_tools_only_expose_safe_response_routes() -> None:
    tools = {
        tool.name: tool
        for tool in build_agent_tools(
            ChargeCatalog.load_default(),
            ReportGuide.load_default(),
        )
    }

    for name in ("send_message", "finish_interaction"):
        properties = tools[name].params_json_schema["properties"]
        assert properties["response_route"]["enum"] == ["reply", "thread"]
        assert set(properties) == {"content", "response_route"}
        assert "<@USER_ID>" in properties["content"]["description"]

    mention_properties = tools["mention_roles"].params_json_schema["properties"]
    assert set(mention_properties) == {"role_ids", "content", "response_route"}
    assert mention_properties["role_ids"]["minItems"] == 1
    assert mention_properties["role_ids"]["maxItems"] == 3
    assert mention_properties["response_route"]["enum"] == ["reply", "thread"]


def test_reaction_tool_exposes_only_message_id_and_emoji() -> None:
    tools = {
        tool.name: tool
        for tool in build_agent_tools(
            ChargeCatalog.load_default(),
            ReportGuide.load_default(),
        )
    }
    schema = tools["set_message_reaction"].params_json_schema
    properties = schema["properties"]

    assert set(properties) == {"message_id", "emoji"}
    assert properties["message_id"]["pattern"] == r"^[1-9][0-9]{16,19}$"
    assert properties["emoji"]["minLength"] == 1
    assert properties["emoji"]["maxLength"] == 100


@pytest.mark.asyncio
async def test_role_mention_tool_is_terminal_and_uses_resolved_ids() -> None:
    publisher = FakePublisher()
    case = ReviewCase(
        guild_id=11111111111111111,
        parent_channel_id=22222222222222222,
        channel_id=22222222222222222,
        root_message_id=33333333333333333,
        trigger_message_id=44444444444444444,
        requester_user_id=55555555555555555,
        interaction_mode=InteractionMode.INITIAL_REVIEW,
        messages=[],
        attachments=[],
    )
    run_context = ReviewRunContext(case=case, publisher=publisher, attempt=1)
    tools = {
        tool.name: tool
        for tool in build_agent_tools(ChargeCatalog.load_default(), ReportGuide.load_default())
    }
    arguments = json.dumps(
        {
            "role_ids": ["66666666666666666", "77777777777777777"],
            "content": "Field Staff, revisen este expediente.",
        }
    )

    result = await tools["mention_roles"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="mention_roles",
            tool_call_id="call-role-mention",
            tool_arguments=arguments,
        ),
        arguments,
    )

    assert publisher.role_mentions == [
        (
            (66666666666666666, 77777777777777777),
            "Field Staff, revisen este expediente.",
        )
    ]
    assert result == {
        "ok": True,
        "delivery": "discord_role_mention",
        "roles_mentioned": 2,
        "messages_sent": 1,
        "response_route": "reply",
        "interaction_complete": True,
    }
    assert run_context.interaction_completed


def test_member_tool_exposes_bounded_projection_without_guild_argument() -> None:
    tools = {
        tool.name: tool
        for tool in build_agent_tools(
            ChargeCatalog.load_default(),
            ReportGuide.load_default(),
        )
    }
    schema = tools["get_guild_members"].params_json_schema
    properties = schema["properties"]

    assert set(properties) == {"user_ids", "fields"}
    assert properties["user_ids"]["minItems"] == 1
    assert properties["user_ids"]["maxItems"] == 10
    assert schema["$defs"]["GuildMemberField"]["enum"] == [
        "display_name",
        "username",
        "roles",
        "joined_at",
        "created_at",
        "is_bot",
        "role_ids",
    ]


def test_member_discovery_tools_are_configurable_and_never_expose_guild_id() -> None:
    tools = {
        tool.name: tool
        for tool in build_agent_tools(
            ChargeCatalog.load_default(),
            ReportGuide.load_default(),
        )
    }

    role_schema = tools["list_guild_roles"].params_json_schema
    assert "guild_id" not in role_schema["properties"]
    assert role_schema["properties"]["limit"]["maximum"] == 25
    assert role_schema["$defs"]["GuildRoleSort"]["enum"] == [
        "position",
        "name",
        "member_count",
    ]

    member_schema = tools["find_guild_members"].params_json_schema
    assert "guild_id" not in member_schema["properties"]
    assert member_schema["properties"]["limit"]["maximum"] == 25
    filters = member_schema["$defs"]["GuildMemberFilters"]["properties"]
    assert set(filters) == {
        "query",
        "user_ids",
        "role_ids_any",
        "role_ids_all",
        "role_ids_none",
        "is_bot",
        "joined_after",
        "joined_before",
    }

    channel_schema = tools["find_channel_members"].params_json_schema
    assert "guild_id" not in channel_schema["properties"]
    assert channel_schema["properties"]["message_limit"]["maximum"] == 200
    assert channel_schema["$defs"]["ChannelMembership"]["enum"] == [
        "can_view",
        "recent_participants",
        "thread_members",
        "voice_connected",
    ]


@pytest.mark.parametrize(
    ("strategy", "candidate_count", "requires_confirmation"),
    [
        ("initial_and_surname", 1, False),
        ("initial_and_surname", 2, True),
        ("surname", 1, True),
    ],
)
def test_member_search_output_marks_weak_or_ambiguous_name_matches(
    strategy: GuildMemberNameMatchStrategy,
    candidate_count: int,
    requires_confirmation: bool,
) -> None:
    output = _member_search_output(
        GuildMemberSearchResult(
            members=(),
            name_match_strategy=strategy,
            candidate_count=candidate_count,
        ),
        ("display_name",),
    )

    assert output["name_match"] == {
        "strategy": strategy,
        "candidate_count": candidate_count,
        "ambiguous": candidate_count > 1,
        "requires_confirmation": requires_confirmation,
    }


@pytest.mark.asyncio
async def test_member_tool_uses_case_guild_deduplicates_and_projects_fields() -> None:
    found_id = 11111111111111111
    missing_id = 22222222222222222
    profile = GuildMemberProfile(
        user_id=found_id,
        display_name="Deputy Torres",
        username="torres",
        roles=("Deputy Sheriff", "GNT"),
        joined_at=datetime(2026, 1, 2, 3, 4, tzinfo=UTC),
        created_at=datetime(2025, 2, 3, 4, 5, tzinfo=UTC),
        is_bot=False,
    )
    directory = FakeMemberDirectory(
        GuildMemberLookupResult(
            members={found_id: profile},
            not_found=frozenset({missing_id}),
            unavailable=frozenset(),
        )
    )
    case = ReviewCase(
        guild_id=98765432109876543,
        parent_channel_id=2,
        channel_id=2,
        root_message_id=3,
        trigger_message_id=3,
        requester_user_id=4,
        interaction_mode=InteractionMode.INITIAL_REVIEW,
        messages=[],
        attachments=[],
    )
    run_context = ReviewRunContext(
        case=case,
        publisher=FakePublisher(),
        attempt=1,
        member_directory=directory,
    )
    tools = {
        tool.name: tool
        for tool in build_agent_tools(ChargeCatalog.load_default(), ReportGuide.load_default())
    }
    arguments = json.dumps(
        {
            "user_ids": [str(found_id), str(found_id), str(missing_id)],
            "fields": ["username", "joined_at", "is_bot"],
        }
    )

    result = await tools["get_guild_members"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="get_guild_members",
            tool_call_id="call-members",
            tool_arguments=arguments,
        ),
        arguments,
    )

    assert directory.calls == [
        (case.guild_id, (found_id, missing_id)),
    ]
    assert result == {
        "ok": True,
        "fields": ["username", "joined_at", "is_bot"],
        "members": [
            {
                "user_id": str(found_id),
                "found": True,
                "username": "torres",
                "joined_at": "2026-01-02T03:04:00Z",
                "is_bot": False,
            },
            {
                "user_id": str(missing_id),
                "found": False,
                "reason": "not_found",
            },
        ],
    }

    default_arguments = json.dumps(
        {
            "user_ids": [str(found_id)],
            "fields": None,
        }
    )
    default_result = await tools["get_guild_members"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="get_guild_members",
            tool_call_id="call-members-default",
            tool_arguments=default_arguments,
        ),
        default_arguments,
    )

    assert default_result == {
        "ok": True,
        "fields": ["display_name", "roles"],
        "members": [
            {
                "user_id": str(found_id),
                "found": True,
                "display_name": "Deputy Torres",
                "roles": ["Deputy Sheriff", "GNT"],
            }
        ],
    }


@pytest.mark.asyncio
async def test_member_discovery_tools_bind_guild_and_channel_to_active_case() -> None:
    guild_id = 98765432109876543
    channel_id = 11111111111111111
    requester_id = 22222222222222222
    user_id = 33333333333333333
    role_id = 44444444444444444
    profile = GuildMemberProfile(
        user_id=user_id,
        display_name="Adriel Beltrán",
        username="adriel",
        roles=("Field Training Officer",),
        joined_at=datetime(2026, 1, 2, 3, 4, tzinfo=UTC),
        created_at=datetime(2025, 2, 3, 4, 5, tzinfo=UTC),
        is_bot=False,
        role_ids=(role_id,),
    )
    directory = FakeDiscoveryDirectory(
        role_result=GuildRoleSearchResult(
            roles=(
                GuildRoleProfile(
                    role_id=role_id,
                    name="Field Training Officer",
                    position=12,
                    managed=False,
                    member_count=4,
                ),
            ),
            next_cursor="roles-next",
        ),
        member_result=GuildMemberSearchResult(
            members=(profile,),
            next_cursor="members-next",
            name_match_strategy="exact_terms",
            candidate_count=1,
        ),
    )
    case = ReviewCase(
        guild_id=guild_id,
        parent_channel_id=channel_id,
        channel_id=channel_id,
        root_message_id=3,
        trigger_message_id=3,
        requester_user_id=requester_id,
        interaction_mode=InteractionMode.INITIAL_REVIEW,
        messages=[],
        attachments=[],
    )
    run_context = ReviewRunContext(
        case=case,
        publisher=FakePublisher(),
        attempt=1,
        member_directory=directory,
    )
    tools = {
        tool.name: tool
        for tool in build_agent_tools(ChargeCatalog.load_default(), ReportGuide.load_default())
    }
    run_context.activity = ActivityRecorder()

    role_arguments = json.dumps(
        {
            "query": "training",
            "role_ids": None,
            "include_managed": False,
            "fields": ["name", "member_count"],
            "sort_by": "member_count",
            "sort_direction": "desc",
            "limit": 5,
            "cursor": None,
        }
    )
    role_output = await tools["list_guild_roles"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="list_guild_roles",
            tool_call_id="call-roles",
            tool_arguments=role_arguments,
        ),
        role_arguments,
    )

    assert directory.role_calls[0][0] == guild_id
    assert directory.role_calls[0][1].query == "training"
    assert role_output == {
        "ok": True,
        "fields": ["name", "member_count"],
        "roles": [
            {
                "role_id": str(role_id),
                "name": "Field Training Officer",
                "member_count": 4,
            }
        ],
        "next_cursor": "roles-next",
    }

    filters = {
        "query": "adriel",
        "user_ids": None,
        "role_ids_any": [str(role_id)],
        "role_ids_all": None,
        "role_ids_none": None,
        "is_bot": False,
        "joined_after": None,
        "joined_before": None,
    }
    member_arguments = json.dumps(
        {
            "filters": filters,
            "fields": ["display_name", "roles", "role_ids"],
            "sort_by": "display_name",
            "sort_direction": "asc",
            "limit": 10,
            "cursor": None,
        }
    )
    member_output = await tools["find_guild_members"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="find_guild_members",
            tool_call_id="call-guild-members",
            tool_arguments=member_arguments,
        ),
        member_arguments,
    )

    assert directory.member_calls[0][0] == guild_id
    assert directory.member_calls[0][1].role_ids_any == (role_id,)
    assert member_output == {
        "ok": True,
        "fields": ["display_name", "roles", "role_ids"],
        "members": [
            {
                "user_id": str(user_id),
                "display_name": "Adriel Beltrán",
                "roles": ["Field Training Officer"],
                "role_ids": [str(role_id)],
            }
        ],
        "next_cursor": "members-next",
        "name_match": {
            "strategy": "exact_terms",
            "candidate_count": 1,
            "ambiguous": False,
            "requires_confirmation": False,
        },
    }

    channel_arguments = json.dumps(
        {
            "channel_id": None,
            "membership": "recent_participants",
            "message_limit": 50,
            "filters": filters,
            "fields": ["display_name", "roles"],
            "sort_by": "display_name",
            "sort_direction": "asc",
            "limit": 10,
            "cursor": None,
        }
    )
    channel_output = await tools["find_channel_members"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="find_channel_members",
            tool_call_id="call-channel-members",
            tool_arguments=channel_arguments,
        ),
        channel_arguments,
    )

    call = directory.channel_member_calls[0]
    assert call[:3] == (guild_id, requester_id, channel_id)
    assert call[3].channel_id == channel_id
    assert call[3].membership == "recent_participants"
    assert channel_output["channel_id"] == str(channel_id)
    assert channel_output["membership"] == "recent_participants"
    assert isinstance(run_context.activity, ActivityRecorder)
    assert run_context.activity.activities == [AgentActivity.PERSONNEL] * 3


def test_message_tool_exposes_selection_filters_without_guild_argument() -> None:
    tools = {
        tool.name: tool
        for tool in build_agent_tools(
            ChargeCatalog.load_default(),
            ReportGuide.load_default(),
        )
    }
    schema = tools["get_messages"].params_json_schema
    properties = schema["properties"]

    assert "guild_id" not in properties
    assert set(properties) == {
        "channel_id",
        "message_id",
        "direction",
        "limit",
        "author_ids",
        "author_type",
        "has_attachments",
        "fields",
    }
    assert schema["$defs"]["MessageDirection"]["enum"] == [
        "exact",
        "before",
        "after",
        "around",
        "latest",
    ]
    assert schema["$defs"]["MessageField"]["enum"] == [
        "content",
        "author",
        "roles",
        "embeds",
        "attachments",
        "reactions",
        "reply_reference",
    ]
    assert properties["limit"]["minimum"] == 1
    assert properties["limit"]["maximum"] == 20


def test_attachment_tool_exposes_only_message_scoped_selection() -> None:
    tools = {
        tool.name: tool
        for tool in build_agent_tools(
            ChargeCatalog.load_default(),
            ReportGuide.load_default(),
        )
    }
    schema = tools["get_message_attachments"].params_json_schema
    properties = schema["properties"]

    assert set(properties) == {"message_id", "channel_id", "attachment_ids"}
    assert "guild_id" not in properties
    assert "url" not in properties
    attachment_list = properties["attachment_ids"]["anyOf"][0]
    assert attachment_list["minItems"] == 1
    assert attachment_list["maxItems"] == 10


@pytest.mark.asyncio
async def test_attachment_tool_loads_omitted_native_inputs_with_run_scope() -> None:
    guild_id = 98765432109876543
    current_channel_id = 11111111111111111
    selected_channel_id = 22222222222222222
    requester_id = 33333333333333333
    message_id = 44444444444444444
    loaded_image_id = 55555555555555555
    omitted_image_id = 66666666666666666
    omitted_file_id = 77777777777777777
    available = (
        MessageAttachmentProfile(
            id=loaded_image_id,
            filename="frontal.png",
            url="https://cdn.discord.test/frontal.png",
            content_type="image/png",
            size=1000,
            width=800,
            height=600,
            input_kind="image",
        ),
        MessageAttachmentProfile(
            id=omitted_image_id,
            filename="cargos.png",
            url="https://cdn.discord.test/cargos.png",
            content_type="image/png",
            size=2000,
            width=1200,
            height=700,
            input_kind="image",
        ),
        MessageAttachmentProfile(
            id=omitted_file_id,
            filename="narrativa.txt",
            url="https://cdn.discord.test/narrativa.txt",
            content_type="text/plain",
            size=3000,
            width=None,
            height=None,
            input_kind="file",
        ),
    )
    directory = FakeMessageDirectory(
        MessageLookupResult(messages=()),
        MessageAttachmentLookupResult(attachments=available),
    )
    case = ReviewCase(
        guild_id=guild_id,
        parent_channel_id=current_channel_id,
        channel_id=current_channel_id,
        root_message_id=message_id,
        trigger_message_id=message_id,
        requester_user_id=requester_id,
        interaction_mode=InteractionMode.INITIAL_REVIEW,
        messages=[],
        attachments=[
            DiscordAttachment(
                id=loaded_image_id,
                message_id=message_id,
                filename="frontal.png",
                url="https://cdn.discord.test/frontal.png",
                content_type="image/png",
                size=1000,
                width=800,
                height=600,
                input_kind="image",
            )
        ],
    )
    run_context = ReviewRunContext(
        case=case,
        publisher=FakePublisher(),
        attempt=1,
        message_directory=directory,
    )
    tools = {
        tool.name: tool
        for tool in build_agent_tools(
            ChargeCatalog.load_default(),
            ReportGuide.load_default(),
            image_detail="original",
        )
    }
    arguments = json.dumps(
        {
            "message_id": str(message_id),
            "channel_id": str(selected_channel_id),
            "attachment_ids": [
                str(loaded_image_id),
                str(omitted_image_id),
                str(omitted_file_id),
            ],
        }
    )

    result = await tools["get_message_attachments"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="get_message_attachments",
            tool_call_id="call-attachments",
            tool_arguments=arguments,
        ),
        arguments,
    )

    assert directory.attachment_calls == [
        (
            guild_id,
            requester_id,
            current_channel_id,
            selected_channel_id,
            message_id,
            (loaded_image_id, omitted_image_id, omitted_file_id),
        )
    ]
    assert isinstance(result, list)
    assert [type(output) for output in result] == [
        ToolOutputText,
        ToolOutputImage,
        ToolOutputImage,
        ToolOutputFileContent,
    ]
    metadata = json.loads(result[0].text)
    assert metadata["loaded"] == [
        {
            "attachment_id": str(loaded_image_id),
            "filename": "frontal.png",
            "kind": "image",
            "size": 1000,
        },
        {
            "attachment_id": str(omitted_image_id),
            "filename": "cargos.png",
            "kind": "image",
            "size": 2000,
        },
        {
            "attachment_id": str(omitted_file_id),
            "filename": "narrativa.txt",
            "kind": "file",
            "size": 3000,
        },
    ]
    assert metadata["skipped"] == []
    assert result[1].image_url == "https://cdn.discord.test/frontal.png"
    assert result[1].detail == "auto"
    assert result[2].image_url == "https://cdn.discord.test/cargos.png"
    assert result[2].detail == "auto"
    assert result[3].file_url == "https://cdn.discord.test/narrativa.txt"
    assert result[3].filename == "narrativa.txt"
    assert run_context.loaded_attachment_ids == {
        loaded_image_id,
        omitted_image_id,
        omitted_file_id,
    }
    assert run_context.loaded_image_count == 2
    assert run_context.loaded_file_count == 1
    assert run_context.loaded_file_bytes == 3000


@pytest.mark.asyncio
async def test_attachment_tool_enforces_limits_and_reports_missing_ids() -> None:
    message_id = 44444444444444444
    image_id = 55555555555555555
    file_id = 66666666666666666
    missing_id = 77777777777777777
    directory = FakeMessageDirectory(
        MessageLookupResult(messages=()),
        MessageAttachmentLookupResult(
            attachments=(
                MessageAttachmentProfile(
                    id=image_id,
                    filename="oversized.png",
                    url="https://cdn.discord.test/oversized.png",
                    content_type="image/png",
                    size=2001,
                    width=800,
                    height=600,
                    input_kind="image",
                ),
                MessageAttachmentProfile(
                    id=file_id,
                    filename="unsupported.zip",
                    url="https://cdn.discord.test/unsupported.zip",
                    content_type="application/zip",
                    size=100,
                    width=None,
                    height=None,
                    input_kind=None,
                ),
            ),
            missing_ids=(missing_id,),
        ),
    )
    case = ReviewCase(
        guild_id=98765432109876543,
        parent_channel_id=11111111111111111,
        channel_id=11111111111111111,
        root_message_id=message_id,
        trigger_message_id=message_id,
        requester_user_id=33333333333333333,
        interaction_mode=InteractionMode.INITIAL_REVIEW,
        messages=[],
        attachments=[],
    )
    run_context = ReviewRunContext(
        case=case,
        publisher=FakePublisher(),
        attempt=1,
        message_directory=directory,
    )
    tools = {
        tool.name: tool
        for tool in build_agent_tools(
            ChargeCatalog.load_default(),
            ReportGuide.load_default(),
            max_image_bytes=2000,
        )
    }
    arguments = json.dumps(
        {
            "message_id": str(message_id),
            "channel_id": None,
            "attachment_ids": [str(image_id), str(file_id), str(missing_id)],
        }
    )

    result = await tools["get_message_attachments"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="get_message_attachments",
            tool_call_id="call-attachments-limited",
            tool_arguments=arguments,
        ),
        arguments,
    )

    assert isinstance(result, list)
    assert len(result) == 1
    assert isinstance(result[0], ToolOutputText)
    metadata = json.loads(result[0].text)
    assert metadata["loaded"] == []
    assert [item["reason"] for item in metadata["skipped"]] == [
        "image_too_large",
        "unsupported_type",
        "not_found_in_message",
    ]


@pytest.mark.asyncio
async def test_attachment_tool_limits_historical_images_loaded_on_demand() -> None:
    root_message_id = 44444444444444444
    trigger_message_id = 44444444444444445
    historical_message_id = 44444444444444443
    first_image_id = 55555555555555551
    second_image_id = 55555555555555552
    directory = FakeMessageDirectory(
        MessageLookupResult(messages=()),
        MessageAttachmentLookupResult(
            attachments=tuple(
                MessageAttachmentProfile(
                    id=image_id,
                    filename=f"historical-{image_id}.png",
                    url=f"https://cdn.discord.test/historical-{image_id}.png",
                    content_type="image/png",
                    size=1000,
                    width=800,
                    height=600,
                    input_kind="image",
                )
                for image_id in (first_image_id, second_image_id)
            )
        ),
    )
    case = ReviewCase(
        guild_id=98765432109876543,
        parent_channel_id=11111111111111111,
        channel_id=root_message_id,
        thread_id=root_message_id,
        root_message_id=root_message_id,
        trigger_message_id=trigger_message_id,
        requester_user_id=33333333333333333,
        interaction_mode=InteractionMode.THREAD_FOLLOW_UP,
        messages=[],
        attachments=[],
    )
    run_context = ReviewRunContext(
        case=case,
        publisher=FakePublisher(),
        attempt=1,
        message_directory=directory,
    )
    tools = {
        tool.name: tool
        for tool in build_agent_tools(
            ChargeCatalog.load_default(),
            ReportGuide.load_default(),
            max_historical_images=1,
        )
    }
    arguments = json.dumps(
        {
            "message_id": str(historical_message_id),
            "attachment_ids": [str(first_image_id), str(second_image_id)],
        }
    )

    result = await tools["get_message_attachments"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="get_message_attachments",
            tool_call_id="call-historical-attachments",
            tool_arguments=arguments,
        ),
        arguments,
    )

    metadata = json.loads(result[0].text)
    assert [item["attachment_id"] for item in metadata["loaded"]] == [str(first_image_id)]
    assert metadata["skipped"] == [
        {
            "attachment_id": str(second_image_id),
            "filename": f"historical-{second_image_id}.png",
            "kind": "image",
            "size": 1000,
            "reason": "historical_image_limit",
        }
    ]
    assert metadata["remaining_capacity"]["historical_images"] == 0
    assert run_context.loaded_image_count == 1
    assert run_context.loaded_historical_image_count == 1


@pytest.mark.asyncio
async def test_attachment_tool_resolves_thread_starter_to_parent_channel() -> None:
    guild_id = 98765432109876543
    parent_channel_id = 11111111111111111
    thread_id = 22222222222222222
    requester_id = 33333333333333333
    attachment_id = 44444444444444444
    directory = FakeMessageDirectory(
        MessageLookupResult(messages=()),
        MessageAttachmentLookupResult(
            attachments=(
                MessageAttachmentProfile(
                    id=attachment_id,
                    filename="reporte.png",
                    url="https://cdn.discord.test/reporte.png",
                    content_type="image/png",
                    size=1000,
                    width=800,
                    height=600,
                    input_kind="image",
                ),
            )
        ),
    )
    case = ReviewCase(
        guild_id=guild_id,
        parent_channel_id=parent_channel_id,
        channel_id=thread_id,
        thread_id=thread_id,
        root_message_id=thread_id,
        trigger_message_id=55555555555555555,
        requester_user_id=requester_id,
        interaction_mode=InteractionMode.THREAD_FOLLOW_UP,
        messages=[],
        attachments=[],
    )
    run_context = ReviewRunContext(
        case=case,
        publisher=FakePublisher(),
        attempt=1,
        access_level=AgentAccessLevel.REPORT_OWNER,
        message_directory=directory,
    )
    tools = {
        tool.name: tool
        for tool in build_agent_tools(
            ChargeCatalog.load_default(),
            ReportGuide.load_default(),
            access_level=AgentAccessLevel.REPORT_OWNER,
        )
    }
    arguments = json.dumps(
        {
            "message_id": str(thread_id),
            "channel_id": str(thread_id),
            "attachment_ids": [str(attachment_id)],
        }
    )

    result = await tools["get_message_attachments"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="get_message_attachments",
            tool_call_id="call-root-attachment",
            tool_arguments=arguments,
        ),
        arguments,
    )

    assert directory.attachment_calls == [
        (
            guild_id,
            requester_id,
            thread_id,
            parent_channel_id,
            thread_id,
            (attachment_id,),
        )
    ]
    assert isinstance(result, list)
    assert isinstance(result[0], ToolOutputText)
    assert json.loads(result[0].text)["channel_id"] == str(parent_channel_id)
    assert isinstance(result[1], ToolOutputImage)


@pytest.mark.asyncio
async def test_message_tool_uses_run_scope_and_projects_results() -> None:
    guild_id = 98765432109876543
    current_channel_id = 11111111111111111
    selected_channel_id = 22222222222222222
    requester_id = 33333333333333333
    anchor_id = 44444444444444444
    author_id = 55555555555555555
    profile = GuildMessageProfile(
        message_id=anchor_id,
        channel_id=selected_channel_id,
        author_id=author_id,
        author_display_name="Deputy Torres",
        author_username="torres",
        author_is_bot=False,
        author_roles=("Deputy Sheriff", "GNT"),
        content="Revisa este antecedente.",
        created_at=datetime(2026, 8, 6, 12, 30, tzinfo=UTC),
        edited_at=None,
        reply_to_message_id=66666666666666666,
        embeds=("Título: Resultado\nDetalle breve",),
        attachments=(
            MessageAttachmentProfile(
                id=77777777777777777,
                filename="evidencia.png",
                url="https://cdn.discord.test/evidencia.png",
                content_type="image/png",
                size=1234,
                width=800,
                height=600,
                input_kind="image",
            ),
        ),
        reactions=(MessageReactionProfile(emoji="✅", count=2, reacted_by_bot=True),),
    )
    directory = FakeMessageDirectory(MessageLookupResult(messages=(profile,)))
    case = ReviewCase(
        guild_id=guild_id,
        parent_channel_id=current_channel_id,
        channel_id=current_channel_id,
        root_message_id=anchor_id,
        trigger_message_id=anchor_id,
        requester_user_id=requester_id,
        interaction_mode=InteractionMode.INITIAL_REVIEW,
        messages=[],
        attachments=[],
    )
    run_context = ReviewRunContext(
        case=case,
        publisher=FakePublisher(),
        attempt=1,
        message_directory=directory,
    )
    tools = {
        tool.name: tool
        for tool in build_agent_tools(ChargeCatalog.load_default(), ReportGuide.load_default())
    }
    run_context.activity = ActivityRecorder()
    arguments = json.dumps(
        {
            "channel_id": str(selected_channel_id),
            "message_id": str(anchor_id),
            "direction": "around",
            "limit": 7,
            "author_ids": [str(author_id), str(author_id)],
            "author_type": "human",
            "has_attachments": True,
            "fields": [
                "content",
                "author",
                "roles",
                "embeds",
                "attachments",
                "reactions",
                "reply_reference",
            ],
        }
    )

    result = await tools["get_messages"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="get_messages",
            tool_call_id="call-messages",
            tool_arguments=arguments,
        ),
        arguments,
    )

    assert directory.calls == [
        (
            guild_id,
            requester_id,
            current_channel_id,
            MessageLookupQuery(
                channel_id=selected_channel_id,
                message_id=anchor_id,
                direction="around",
                limit=7,
                author_ids=(author_id,),
                author_type="human",
                has_attachments=True,
            ),
        )
    ]
    assert result == {
        "ok": True,
        "channel_id": str(selected_channel_id),
        "direction": "around",
        "fields": [
            "content",
            "author",
            "roles",
            "embeds",
            "attachments",
            "reactions",
            "reply_reference",
        ],
        "messages": [
            {
                "message_id": str(anchor_id),
                "channel_id": str(selected_channel_id),
                "timestamp": "2026-08-06T12:30:00Z",
                "content": "Revisa este antecedente.",
                "author": {
                    "user_id": str(author_id),
                    "display_name": "Deputy Torres",
                    "username": "torres",
                    "is_bot": False,
                },
                "roles": ["Deputy Sheriff", "GNT"],
                "embeds": ["Título: Resultado\nDetalle breve"],
                "attachments": [
                    {
                        "attachment_id": "77777777777777777",
                        "filename": "evidencia.png",
                        "content_type": "image/png",
                        "size": 1234,
                        "width": 800,
                        "height": 600,
                    }
                ],
                "reactions": [
                    {
                        "emoji": "✅",
                        "count": 2,
                        "reacted_by_bot": True,
                    }
                ],
                "reply_reference": "66666666666666666",
            }
        ],
        "truncated": False,
    }

    default_arguments = json.dumps(
        {
            "channel_id": None,
            "message_id": None,
            "direction": "latest",
            "limit": 3,
            "author_ids": None,
            "author_type": "any",
            "has_attachments": None,
            "fields": None,
        }
    )
    default_result = await tools["get_messages"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="get_messages",
            tool_call_id="call-messages-default",
            tool_arguments=default_arguments,
        ),
        default_arguments,
    )

    assert directory.calls[-1][3] == MessageLookupQuery(
        channel_id=current_channel_id,
        message_id=None,
        direction="latest",
        limit=3,
        author_ids=(),
        author_type="any",
        has_attachments=None,
    )
    assert default_result["fields"] == [
        "content",
        "author",
        "embeds",
        "attachments",
    ]
    assert isinstance(run_context.activity, ActivityRecorder)
    assert run_context.activity.activities == [AgentActivity.EVIDENCE] * 2


@pytest.mark.asyncio
async def test_message_tool_rejects_incompatible_anchor_and_direction() -> None:
    case = ReviewCase(
        guild_id=98765432109876543,
        parent_channel_id=11111111111111111,
        channel_id=11111111111111111,
        root_message_id=22222222222222222,
        trigger_message_id=22222222222222222,
        requester_user_id=33333333333333333,
        interaction_mode=InteractionMode.INITIAL_REVIEW,
        messages=[],
        attachments=[],
    )
    directory = FakeMessageDirectory(MessageLookupResult(messages=()))
    run_context = ReviewRunContext(
        case=case,
        publisher=FakePublisher(),
        attempt=1,
        message_directory=directory,
    )
    tools = {
        tool.name: tool
        for tool in build_agent_tools(ChargeCatalog.load_default(), ReportGuide.load_default())
    }
    arguments = json.dumps(
        {
            "channel_id": None,
            "message_id": "22222222222222222",
            "direction": "latest",
            "limit": 5,
            "author_ids": None,
            "author_type": "any",
            "has_attachments": None,
            "fields": None,
        }
    )

    result = await tools["get_messages"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="get_messages",
            tool_call_id="call-invalid-messages",
            tool_arguments=arguments,
        ),
        arguments,
    )

    assert result == {
        "ok": False,
        "error": "invalid_message_query",
        "messages": [],
    }
    assert directory.calls == []


@pytest.mark.asyncio
async def test_discord_tools_execute_side_effects_immediately() -> None:
    publisher = FakePublisher()
    case = ReviewCase(
        guild_id=1,
        parent_channel_id=2,
        channel_id=2,
        root_message_id=3,
        trigger_message_id=3,
        requester_user_id=4,
        interaction_mode=InteractionMode.INITIAL_REVIEW,
        messages=[],
        attachments=[],
    )
    activity = ActivityRecorder()
    run_context = ReviewRunContext(
        case=case,
        publisher=publisher,
        attempt=1,
        activity=activity,
        request_intent=RequestIntent.REVIEW,
    )
    tools = {
        tool.name: tool
        for tool in build_agent_tools(ChargeCatalog.load_default(), ReportGuide.load_default())
    }

    publish_arguments = json.dumps(
        {
            "publication": {
                "verdict": "approved",
                "summary": "El expediente es coherente.",
                "findings": [],
                "charges": [],
            }
        }
    )
    publish_context = ToolContext(
        context=run_context,
        tool_name="publish_review",
        tool_call_id="call-publish",
        tool_arguments=publish_arguments,
    )
    result = await tools["publish_review"].on_invoke_tool(
        publish_context,
        publish_arguments,
    )

    assert publisher.publications[0].verdict is ReviewVerdict.APPROVED
    assert publisher.markers == 0
    assert run_context.published
    assert result == {
        "ok": True,
        "review_scope": "full",
        "verdict": "approved",
    }
    assert activity.activities == [AgentActivity.RESPONSE]


@pytest.mark.asyncio
async def test_plain_conversation_cannot_publish_a_review() -> None:
    publisher = FakePublisher()
    case = ReviewCase(
        guild_id=1,
        parent_channel_id=2,
        channel_id=2,
        root_message_id=3,
        trigger_message_id=3,
        requester_user_id=4,
        interaction_mode=InteractionMode.THREAD_FOLLOW_UP,
        messages=[],
        attachments=[],
    )
    run_context = ReviewRunContext(
        case=case,
        publisher=publisher,
        attempt=1,
        request_intent=RequestIntent.CONVERSATION,
    )
    tools = {
        tool.name: tool
        for tool in build_agent_tools(ChargeCatalog.load_default(), ReportGuide.load_default())
    }
    arguments = json.dumps(
        {
            "publication": {
                "verdict": "approved",
                "summary": "Esto no debe convertirse en un veredicto.",
                "findings": [],
                "charges": [],
            }
        }
    )

    result = await tools["publish_review"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="publish_review",
            tool_call_id="call-conversation-publish",
            tool_arguments=arguments,
        ),
        arguments,
    )

    assert result == {
        "ok": False,
        "error": "review_not_requested",
        "interaction_complete": False,
    }
    assert publisher.publications == []
    assert run_context.published is False


@pytest.mark.asyncio
async def test_reaction_tool_updates_discord_immediately_without_finishing() -> None:
    publisher = FakePublisher()
    root_message_id = 33333333333333333
    case = ReviewCase(
        guild_id=11111111111111111,
        parent_channel_id=22222222222222222,
        channel_id=22222222222222222,
        root_message_id=root_message_id,
        trigger_message_id=44444444444444444,
        requester_user_id=55555555555555555,
        interaction_mode=InteractionMode.THREAD_FOLLOW_UP,
        messages=[],
        attachments=[],
    )
    run_context = ReviewRunContext(case=case, publisher=publisher, attempt=1)
    tools = {
        tool.name: tool
        for tool in build_agent_tools(ChargeCatalog.load_default(), ReportGuide.load_default())
    }
    arguments = json.dumps({"message_id": str(root_message_id), "emoji": "✅"})

    result = await tools["set_message_reaction"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="set_message_reaction",
            tool_call_id="call-reaction",
            tool_arguments=arguments,
        ),
        arguments,
    )

    assert publisher.reactions == [(root_message_id, "✅")]
    assert result == {
        "ok": True,
        "message_id": str(root_message_id),
        "emoji": "✅",
        "outcome": "replaced",
        "reaction_updates": 1,
        "interaction_complete": False,
    }
    assert run_context.reactions_updated_count == 1
    assert not run_context.interaction_completed
    assert not run_context.published


@pytest.mark.asyncio
async def test_messages_can_be_sent_before_explicit_conversational_completion() -> None:
    publisher = FakePublisher()
    case = ReviewCase(
        guild_id=1,
        parent_channel_id=2,
        channel_id=2,
        root_message_id=3,
        trigger_message_id=3,
        requester_user_id=4,
        interaction_mode=InteractionMode.INITIAL_REVIEW,
        messages=[],
        attachments=[],
    )
    run_context = ReviewRunContext(case=case, publisher=publisher, attempt=1)
    tools = {
        tool.name: tool
        for tool in build_agent_tools(ChargeCatalog.load_default(), ReportGuide.load_default())
    }
    progress_message = "📚 Estoy contrastando ambos cargos, deputy."
    progress_arguments = json.dumps({"content": progress_message})
    tool_context = ToolContext(
        context=run_context,
        tool_name="send_message",
        tool_call_id="call-message",
        tool_arguments=progress_arguments,
    )

    first_result = await tools["send_message"].on_invoke_tool(
        tool_context,
        progress_arguments,
    )
    second_progress = "Ya tengo las definiciones; estoy preparando la diferencia práctica."
    second_arguments = json.dumps({"content": second_progress, "response_route": "thread"})
    await tools["send_message"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="send_message",
            tool_call_id="call-message-2",
            tool_arguments=second_arguments,
        ),
        second_arguments,
    )

    assert publisher.messages == [progress_message, second_progress]
    assert first_result == {
        "ok": True,
        "delivery": "discord_message",
        "messages_sent": 1,
        "response_route": "reply",
        "interaction_complete": False,
    }
    assert publisher.routes == [
        ConversationResponseRoute.REPLY,
        ConversationResponseRoute.REPLY,
    ]
    assert run_context.message_sent
    assert run_context.messages_sent_count == 2
    assert not run_context.interaction_completed
    assert not run_context.published

    final_message = "### Diferencia clave\nLa intención específica de matar distingue el intento."
    final_arguments = json.dumps({"content": final_message})
    final_result = await tools["finish_interaction"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="finish_interaction",
            tool_call_id="call-finish",
            tool_arguments=final_arguments,
        ),
        final_arguments,
    )

    assert publisher.messages[-1] == final_message
    assert final_result == {
        "ok": True,
        "delivery": "discord_message",
        "messages_sent": 3,
        "response_route": "reply",
        "interaction_complete": True,
    }
    assert run_context.messages_sent_count == 3
    assert run_context.interaction_completed


@pytest.mark.asyncio
async def test_knowledge_tools_promote_the_interaction_activity() -> None:
    activity = ActivityRecorder()
    case = ReviewCase(
        guild_id=1,
        parent_channel_id=2,
        channel_id=2,
        root_message_id=3,
        trigger_message_id=3,
        requester_user_id=4,
        interaction_mode=InteractionMode.INITIAL_REVIEW,
        messages=[],
        attachments=[],
    )
    run_context = ReviewRunContext(
        case=case,
        publisher=FakePublisher(),
        attempt=1,
        activity=activity,
    )
    tools = {
        tool.name: tool
        for tool in build_agent_tools(ChargeCatalog.load_default(), ReportGuide.load_default())
    }
    arguments = json.dumps({"queries": ["211", "10 15", "Ocean"]})

    result = await tools["search_radio_codes"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="search_radio_codes",
            tool_call_id="call-knowledge",
            tool_arguments=arguments,
        ),
        arguments,
    )

    assert activity.activities == [AgentActivity.KNOWLEDGE]
    assert {item["code"] for item in result["results"]} == {"211", "10-15", "O"}


@pytest.mark.asyncio
async def test_manual_tools_navigate_search_and_read_with_distinct_activities() -> None:
    activity = ActivityRecorder()
    case = ReviewCase(
        guild_id=1,
        parent_channel_id=2,
        channel_id=2,
        root_message_id=3,
        trigger_message_id=3,
        requester_user_id=4,
        interaction_mode=InteractionMode.INITIAL_REVIEW,
        messages=[],
        attachments=[],
    )
    run_context = ReviewRunContext(
        case=case,
        publisher=FakePublisher(),
        attempt=1,
        activity=activity,
    )
    tools = {
        tool.name: tool
        for tool in build_agent_tools(ChargeCatalog.load_default(), ReportGuide.load_default())
    }

    list_arguments = json.dumps({"parent_id": "053-comunicacion", "limit": 10})
    listed = await tools["list_manual_sections"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="list_manual_sections",
            tool_call_id="call-manual-index",
            tool_arguments=list_arguments,
        ),
        list_arguments,
    )
    search_arguments = json.dumps(
        {
            "queries": ["pérdida de observación"],
            "section_ids": ["053-comunicacion"],
            "match_mode": "all",
        }
    )
    searched = await tools["search_manual"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="search_manual",
            tool_call_id="call-manual-search",
            tool_arguments=search_arguments,
        ),
        search_arguments,
    )
    section_id = searched["results"][0]["section_id"]
    read_arguments = json.dumps({"section_id": section_id, "max_chars": 500})
    read = await tools["read_manual"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="read_manual",
            tool_call_id="call-manual-read",
            tool_arguments=read_arguments,
        ),
        read_arguments,
    )

    assert listed["matched"] == 6
    assert section_id == "069-perdida-de-obs-en-persecucion"
    assert read["text"]
    assert activity.activities == [
        AgentActivity.MANUAL_INDEX,
        AgentActivity.MANUAL_SEARCH,
        AgentActivity.MANUAL_READ,
    ]


@pytest.mark.asyncio
async def test_manual_tools_are_full_access_only_and_enforce_per_run_limits() -> None:
    full_tools = {
        tool.name: tool
        for tool in build_agent_tools(ChargeCatalog.load_default(), ReportGuide.load_default())
    }
    owner_names = {
        tool.name
        for tool in build_agent_tools(
            ChargeCatalog.load_default(),
            ReportGuide.load_default(),
            access_level=AgentAccessLevel.REPORT_OWNER,
        )
    }
    assert {
        "list_manual_sections",
        "search_manual",
        "read_manual",
        "publish_manual_question",
    }.isdisjoint(owner_names)

    run_context = ReviewRunContext(
        case=ReviewCase(
            guild_id=1,
            parent_channel_id=2,
            channel_id=2,
            root_message_id=3,
            trigger_message_id=3,
            requester_user_id=4,
            interaction_mode=InteractionMode.INITIAL_REVIEW,
            messages=[],
            attachments=[],
        ),
        publisher=FakePublisher(),
        attempt=1,
    )
    arguments = json.dumps({})
    result: dict[str, object] = {}
    for index in range(4):
        result = await full_tools["list_manual_sections"].on_invoke_tool(
            ToolContext(
                context=run_context,
                tool_name="list_manual_sections",
                tool_call_id=f"manual-list-{index}",
                tool_arguments=arguments,
            ),
            arguments,
        )

    assert result == {"ok": False, "error": "run_manual_list_limit_reached"}


@pytest.mark.asyncio
async def test_dynamic_manual_question_uses_defaults_without_finishing_interaction() -> None:
    publisher = FakePublisher()
    activity = ActivityRecorder()
    run_context = ReviewRunContext(
        case=ReviewCase(
            guild_id=1,
            parent_channel_id=2,
            channel_id=2,
            root_message_id=3,
            trigger_message_id=3,
            requester_user_id=4,
            interaction_mode=InteractionMode.INITIAL_REVIEW,
            messages=[],
            attachments=[],
        ),
        publisher=publisher,
        attempt=1,
        activity=activity,
    )
    tools = {
        tool.name: tool
        for tool in build_agent_tools(ChargeCatalog.load_default(), ReportGuide.load_default())
    }
    arguments = json.dumps(
        {
            "question": {
                "prompt": "¿Qué debe priorizar un deputy al perder la visual en una persecución?",
                "options": [
                    "Comunicar la pérdida y la última ubicación conocida",
                    "Mantener silencio hasta recuperar contacto visual",
                    "Cambiar de frecuencia antes de informar",
                ],
                "correct_option_number": 1,
                "explanation": "Central necesita conocer la pérdida y la última ubicación.",
            }
        }
    )

    result = await tools["publish_manual_question"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="publish_manual_question",
            tool_call_id="call-dynamic-manual-question",
            tool_arguments=arguments,
        ),
        arguments,
    )

    publication = publisher.manual_quizzes[0]
    assert publication.answer_mode is QuizAnswerMode.SINGLE_CHOICE
    assert publication.category is QuizCategory.GENERAL
    assert publication.difficulty is QuizDifficulty.INTERMEDIATE
    assert publication.duration_seconds is QuizDurationSeconds.THIRTY
    assert publication.source_reference == "Manual faccionario"
    assert result == {
        "ok": True,
        "status": "published",
        "question_id": "dynamic-question-id",
        "retry_after_seconds": None,
        "delivery": "discord_manual_quiz",
        "interaction_complete": False,
    }
    assert not run_context.interaction_completed
    assert run_context.messages_sent_count == 1

    follow_up_arguments = json.dumps({})
    follow_up = await tools["list_manual_sections"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="list_manual_sections",
            tool_call_id="call-after-dynamic-question",
            tool_arguments=follow_up_arguments,
        ),
        follow_up_arguments,
    )

    assert follow_up["ok"] is True
    assert activity.activities == [AgentActivity.RESPONSE, AgentActivity.MANUAL_INDEX]


def test_poll_tools_are_full_access_only_and_expose_flexible_bounded_schemas() -> None:
    full_tools = {
        tool.name: tool
        for tool in build_agent_tools(ChargeCatalog.load_default(), ReportGuide.load_default())
    }
    owner_names = {
        tool.name
        for tool in build_agent_tools(
            ChargeCatalog.load_default(),
            ReportGuide.load_default(),
            access_level=AgentAccessLevel.REPORT_OWNER,
        )
    }

    assert {"create_poll", "list_polls", "get_poll", "end_poll"}.isdisjoint(owner_names)
    create_schema = full_tools["create_poll"].params_json_schema
    properties = create_schema["properties"]
    assert set(properties) == {
        "question",
        "answers",
        "duration_hours",
        "allow_multiselect",
        "silent",
        "intro",
        "channel_id",
    }
    assert properties["answers"]["minItems"] == 2
    assert properties["answers"]["maxItems"] == 10
    assert properties["duration_hours"]["minimum"] == 1
    assert properties["duration_hours"]["maximum"] == 768
    assert properties["duration_hours"]["default"] == 24
    assert properties["allow_multiselect"]["default"] is False
    assert properties["silent"]["default"] is False


@pytest.mark.asyncio
async def test_poll_tools_forward_composable_arguments_without_finishing_the_run() -> None:
    manager = FakePollManager()
    activity = ActivityRecorder()
    run_context = ReviewRunContext(
        case=ReviewCase(
            guild_id=11111111111111111,
            parent_channel_id=22222222222222222,
            channel_id=22222222222222222,
            root_message_id=33333333333333333,
            trigger_message_id=44444444444444444,
            requester_user_id=55555555555555555,
            interaction_mode=InteractionMode.INITIAL_REVIEW,
            messages=[],
            attachments=[],
        ),
        publisher=FakePublisher(),
        attempt=1,
        activity=activity,
        poll_manager=manager,
    )
    tools = {
        tool.name: tool
        for tool in build_agent_tools(ChargeCatalog.load_default(), ReportGuide.load_default())
    }
    create_arguments = json.dumps(
        {
            "question": "¿Qué horario prefieren?",
            "answers": [
                {"text": "Turno mañana", "emoji": "🌅"},
                {"text": "Turno noche", "emoji": "<:night:12345678901234567>"},
            ],
            "duration_hours": 72,
            "allow_multiselect": True,
            "silent": True,
            "intro": "Elijan todos los horarios que les sirven.",
            "channel_id": "66666666666666666",
        }
    )
    created = await tools["create_poll"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="create_poll",
            tool_call_id="poll-create",
            tool_arguments=create_arguments,
        ),
        create_arguments,
    )
    list_arguments = json.dumps(
        {
            "status": "open",
            "creator_user_ids": ["55555555555555555"],
            "channel_ids": ["66666666666666666"],
            "created_after": "2026-08-01T00:00:00-04:00",
            "expires_before": "2026-09-01T00:00:00Z",
            "sort_direction": "asc",
            "page": 2,
            "limit": 5,
        }
    )
    await tools["list_polls"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="list_polls",
            tool_call_id="poll-list",
            tool_arguments=list_arguments,
        ),
        list_arguments,
    )
    get_arguments = json.dumps(
        {
            "message_id": "66666666666666666",
            "include_voters": True,
            "answer_ids": [2, 1, 2],
            "voter_limit_per_answer": 40,
            "after_user_id": "77777777777777777",
        }
    )
    await tools["get_poll"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="get_poll",
            tool_call_id="poll-get",
            tool_arguments=get_arguments,
        ),
        get_arguments,
    )
    end_arguments = json.dumps({"message_id": "66666666666666666"})
    ended = await tools["end_poll"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="end_poll",
            tool_call_id="poll-end",
            tool_arguments=end_arguments,
        ),
        end_arguments,
    )

    request = manager.created[0][4]
    query = manager.listed[0][2]
    assert request.duration_hours == 72
    assert request.allow_multiselect is True
    assert request.answers[1].emoji == "<:night:12345678901234567>"
    assert query.created_after == datetime(2026, 8, 1, 4, tzinfo=UTC)
    assert query.expires_before == datetime(2026, 9, 1, tzinfo=UTC)
    assert query.page == 2
    assert manager.read == [
        (
            11111111111111111,
            55555555555555555,
            66666666666666666,
            True,
            (2, 1),
            40,
            77777777777777777,
        )
    ]
    assert manager.ended == [
        (11111111111111111, 55555555555555555, 66666666666666666)
    ]
    assert created["interaction_complete"] is False
    assert ended["interaction_complete"] is False
    assert not run_context.interaction_completed
    assert activity.activities == [
        AgentActivity.RESPONSE,
        AgentActivity.KNOWLEDGE,
        AgentActivity.KNOWLEDGE,
        AgentActivity.RESPONSE,
    ]


@pytest.mark.asyncio
async def test_create_poll_rejects_duplicate_answers_before_discord() -> None:
    manager = FakePollManager()
    run_context = ReviewRunContext(
        case=ReviewCase(
            guild_id=11111111111111111,
            parent_channel_id=22222222222222222,
            channel_id=22222222222222222,
            root_message_id=33333333333333333,
            trigger_message_id=44444444444444444,
            requester_user_id=55555555555555555,
            interaction_mode=InteractionMode.INITIAL_REVIEW,
            messages=[],
            attachments=[],
        ),
        publisher=FakePublisher(),
        attempt=1,
        poll_manager=manager,
    )
    tool = {
        item.name: item
        for item in build_agent_tools(ChargeCatalog.load_default(), ReportGuide.load_default())
    }["create_poll"]
    arguments = json.dumps(
        {
            "question": "¿Una opción?",
            "answers": [{"text": "Sí"}, {"text": "SÍ"}],
        }
    )

    result = await tool.on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="create_poll",
            tool_call_id="poll-duplicate",
            tool_arguments=arguments,
        ),
        arguments,
    )

    assert result == {"ok": False, "error": "duplicate_answers"}
    assert manager.created == []


@pytest.mark.asyncio
async def test_charge_index_batches_independent_queries_in_one_tool_call() -> None:
    run_context = ReviewRunContext(
        case=ReviewCase(
            guild_id=11111111111111111,
            parent_channel_id=22222222222222222,
            channel_id=22222222222222222,
            root_message_id=33333333333333333,
            trigger_message_id=44444444444444444,
            requester_user_id=55555555555555555,
            interaction_mode=InteractionMode.INITIAL_REVIEW,
            messages=[],
            attachments=[],
        ),
        publisher=FakePublisher(),
        attempt=1,
    )
    tool = {
        item.name: item
        for item in build_agent_tools(ChargeCatalog.load_default(), ReportGuide.load_default())
    }["list_charge_index"]
    arguments = json.dumps(
        {
            "queries": ["CP-189/664", "CP-29800", "CV-2801"],
            "fields": ["code"],
            "match_mode": "all",
            "limit": 5,
        }
    )

    result = await tool.on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="list_charge_index",
            tool_call_id="batched-charge-index",
            tool_arguments=arguments,
        ),
        arguments,
    )

    assert result["query_count"] == 3
    assert [item["query"] for item in result["queries"]] == [
        "CP-189/664",
        "CP-29800",
        "CV-2801",
    ]
    assert all(item["returned"] >= 1 for item in result["queries"])
    assert [item["results"][0]["code"] for item in result["queries"]] == [
        "CP-189/664",
        "CP-29800",
        "CV-2801",
    ]


@pytest.mark.asyncio
async def test_charge_tools_keep_the_catalog_snapshot_pinned_to_the_agent_run() -> None:
    first = ChargeCatalogSnapshot(_catalog(901, "Catálogo inicial"), ChargeCatalogSource.SARP)
    second = ChargeCatalogSnapshot(_catalog(902, "Catálogo nuevo"), ChargeCatalogSource.SARP)
    provider = MutableCatalogProvider(first)
    case = ReviewCase(
        guild_id=11111111111111111,
        parent_channel_id=22222222222222222,
        channel_id=22222222222222222,
        root_message_id=33333333333333333,
        trigger_message_id=44444444444444444,
        requester_user_id=55555555555555555,
        interaction_mode=InteractionMode.INITIAL_REVIEW,
        messages=[],
        attachments=[],
    )
    run_context = ReviewRunContext(
        case=case,
        publisher=FakePublisher(),
        attempt=1,
        charge_catalog_snapshot=provider.snapshot(),
    )
    provider.current = second
    tools = {
        tool.name: tool
        for tool in build_agent_tools(provider, ReportGuide.load_default())
    }
    arguments = json.dumps({"charge_ids": [901]})

    result = await tools["get_charge_details"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="get_charge_details",
            tool_call_id="pinned-catalog",
            tool_arguments=arguments,
        ),
        arguments,
    )

    assert result["charges"][0]["title"] == "Catálogo inicial"
    assert result["missing_ids"] == []
