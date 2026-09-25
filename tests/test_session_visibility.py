from __future__ import annotations

from functools import partial

import pytest
from test_agent_sessions import FakeRepository, FakeSession

from arrest_report_agent.agent_sessions import AgentConversationKey, AgentSessionManager
from arrest_report_agent.application.session_visibility import owner_visible_session_items


def _view(items, *, suspect_name="Dustin Vogel", report_id=8215):
    return owner_visible_session_items(
        items,
        owner_session_id="owner",
        suspect_name=suspect_name,
        report_id=report_id,
        parent_channel_id=10,
        root_message_id=20,
        thread_id=30,
    )


def test_owner_gets_report_facts_but_not_private_staff_context() -> None:
    items = [
        ("owner", {"role": "user", "content": "revisa mi reporte"}),
        ("staff", {"role": "user", "content": "prompt con contexto interno"}),
        (
            "staff",
            {
                "type": "function_call",
                "name": "get_character_legal_facts",
                "call_id": "fines",
                "arguments": '{"character_name":"Dústin_Vogel","exclude_arrest_id":8215}',
            },
        ),
        ("staff", {"type": "function_call_output", "call_id": "fines", "output": "dos multas"}),
        (
            "staff",
            {
                "type": "function_call",
                "name": "get_messages",
                "call_id": "other-channel",
                "arguments": '{"channel_id":"999","direction":"latest"}',
            },
        ),
        (
            "staff",
            {"type": "function_call_output", "call_id": "other-channel", "output": "dato privado"},
        ),
        ("staff", {"role": "assistant", "content": "borrador no necesariamente publicado"}),
    ]

    result = _view(items)

    assert result == [items[0][1], items[2][1], items[3][1]]
    assert "dato privado" not in str(result)


@pytest.mark.parametrize(
    "arguments",
    [
        '{"character_name":"Otra Persona","exclude_arrest_id":8215}',
        '{"character_name":"Dustin Vogel","exclude_arrest_id":9999}',
        '{"character_name":"Dustin Vogel"}',
        "not json",
    ],
)
def test_other_character_or_report_facts_are_not_shared(arguments: str) -> None:
    items = [
        (
            "staff",
            {
                "type": "function_call",
                "name": "get_character_legal_facts",
                "call_id": "facts",
                "arguments": arguments,
            },
        ),
        ("staff", {"type": "function_call_output", "call_id": "facts", "output": "privado"}),
    ]

    assert _view(items) == []


def test_owner_only_sees_scoped_report_and_discord_reads() -> None:
    calls = [
        ("get_report_guide", "{}", True),
        ("get_current_arrest_report", "{}", True),
        ("get_report_supporting_images", '{"report_id":8215}', True),
        ("get_report_supporting_images", '{"report_id":1234}', False),
        ("get_messages", '{"channel_id":30,"direction":"latest"}', True),
        ("get_messages", '{"channel_id":10,"message_id":20,"direction":"exact"}', True),
        ("get_messages", '{"channel_id":10,"direction":"latest"}', False),
        ("get_message_attachments", '{"channel_id":10,"message_id":20}', True),
        ("get_message_attachments", '{"channel_id":10,"message_id":21}', False),
        ("get_faction_frequencies", "{}", False),
    ]
    items = []
    for index, (name, arguments, _) in enumerate(calls):
        call_id = f"call-{index}"
        items.extend(
            [
                (
                    "staff",
                    {"type": "function_call", "name": name, "call_id": call_id, "arguments": arguments},
                ),
                ("staff", {"type": "function_call_output", "call_id": call_id, "output": name}),
            ]
        )

    visible_call_ids = {
        item["call_id"] for item in _view(items) if item["type"] == "function_call"
    }

    assert visible_call_ids == {
        f"call-{index}" for index, (_, _, allowed) in enumerate(calls) if allowed
    }


@pytest.mark.asyncio
async def test_owner_session_reads_safe_staff_tools_without_inheriting_staff_cursor() -> None:
    repository = FakeRepository()
    sessions: dict[str, FakeSession] = {}

    def factory(session_id: str) -> FakeSession:
        return sessions.setdefault(session_id, FakeSession(session_id, repository.history))

    manager = AgentSessionManager(  # type: ignore[arg-type]
        object(),
        repository=repository,  # type: ignore[arg-type]
        session_factory=factory,  # type: ignore[arg-type]
    )
    owner_key = AgentConversationKey("discord_agent_thread", "guild:1:root:20", "report_owner")
    staff_key = AgentConversationKey("discord_agent_thread", "guild:1:root:20", "full")
    owner = await manager.prepare(owner_key, next_source_cursor="30")
    await owner.session.add_items([{"role": "user", "content": "revisión inicial"}])  # type: ignore[arg-type]
    staff = await manager.prepare(staff_key, next_source_cursor="40")
    await staff.session.add_items(  # type: ignore[arg-type]
        [
            {
                "type": "function_call",
                "name": "get_character_legal_facts",
                "call_id": "facts",
                "arguments": '{"character_name":"Dustin Vogel","exclude_arrest_id":8215}',
            },
            {"type": "function_call_output", "call_id": "facts", "output": "dos multas"},
        ]
    )
    resumed = await manager.prepare(
        owner_key,
        next_source_cursor="50",
        readable_partitions=("full",),
        history_filter=partial(
            owner_visible_session_items,
            owner_session_id=owner_key.session_id,
            suspect_name="Dustin Vogel",
            report_id=8215,
            parent_channel_id=10,
            root_message_id=20,
            thread_id=30,
        ),
    )

    assert resumed.source_cursor == "30"
    assert resumed.readable_source_cursors == ("40",)
    assert [item.get("call_id") for item in await resumed.session.get_items()] == [
        None,
        "facts",
        "facts",
    ]
