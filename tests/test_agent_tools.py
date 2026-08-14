from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from agents import ToolOutputFileContent, ToolOutputImage, ToolOutputText
from agents.tool_context import ToolContext

from arrest_report_agent.application.ports import (
    ChannelMemberSearchQuery,
    GuildMemberLookupResult,
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
)
from arrest_report_agent.application.service_context import ReviewRunContext
from arrest_report_agent.application.tools import build_agent_tools
from arrest_report_agent.domain import (
    AgentAccessLevel,
    AgentActivity,
    ConversationResponseRoute,
    DiscordAttachment,
    InteractionMode,
    ReviewCase,
    ReviewVerdict,
)
from arrest_report_agent.knowledge import ChargeCatalog, ReportGuide


class FakePublisher:
    def __init__(self) -> None:
        self.markers = 0
        self.publications = []
        self.messages: list[str] = []
        self.routes: list[ConversationResponseRoute] = []
        self.reactions: list[tuple[int, str]] = []

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

    async def set_message_reaction(self, message_id: int, emoji: str) -> str:
        self.reactions.append((message_id, emoji))
        return "replaced"

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

    async def promote(self, activity: AgentActivity) -> None:
        self.activities.append(activity)


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
        ToolOutputFileContent,
    ]
    metadata = json.loads(result[0].text)
    assert metadata["loaded"] == [
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
    assert metadata["skipped"] == [
        {
            "attachment_id": str(loaded_image_id),
            "filename": "frontal.png",
            "kind": "image",
            "size": 1000,
            "reason": "already_loaded",
        }
    ]
    assert result[1].image_url == "https://cdn.discord.test/cargos.png"
    assert result[1].detail == "auto"
    assert result[2].file_url == "https://cdn.discord.test/narrativa.txt"
    assert result[2].filename == "narrativa.txt"
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
    run_context = ReviewRunContext(case=case, publisher=publisher, attempt=1)
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
