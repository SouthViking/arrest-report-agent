from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from agents.tool_context import ToolContext

from arrest_report_agent.application.ports import (
    GuildMemberLookupResult,
    GuildMemberProfile,
    GuildMessageProfile,
    MessageAttachmentProfile,
    MessageLookupQuery,
    MessageLookupResult,
    MessageReactionProfile,
)
from arrest_report_agent.application.service_context import ReviewRunContext
from arrest_report_agent.application.tools import build_agent_tools
from arrest_report_agent.domain import (
    AgentActivity,
    ConversationResponseRoute,
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

    async def publish_failure(self, public_message) -> None:
        raise AssertionError("not expected")


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


class FakeMessageDirectory:
    def __init__(self, result: MessageLookupResult) -> None:
        self.result = result
        self.calls: list[tuple[int, int, int, MessageLookupQuery]] = []

    async def lookup_messages(
        self,
        guild_id: int,
        requester_user_id: int,
        current_channel_id: int,
        query: MessageLookupQuery,
    ) -> MessageLookupResult:
        self.calls.append((guild_id, requester_user_id, current_channel_id, query))
        return self.result


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
                content_type="image/png",
                size=1234,
                width=800,
                height=600,
            ),
        ),
        reactions=(
            MessageReactionProfile(emoji="✅", count=2, reacted_by_bot=True),
        ),
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
    second_arguments = json.dumps(
        {"content": second_progress, "response_route": "thread"}
    )
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
    arguments = json.dumps({"charge_ids": [1]})

    await tools["get_charge_details"].on_invoke_tool(
        ToolContext(
            context=run_context,
            tool_name="get_charge_details",
            tool_call_id="call-knowledge",
            tool_arguments=arguments,
        ),
        arguments,
    )

    assert activity.activities == [AgentActivity.KNOWLEDGE]
