from __future__ import annotations

import json
from copy import deepcopy
from functools import partial

import pytest
from test_agent_sessions import FakeRepository, FakeSession

from arrest_report_agent.agent_sessions import AgentConversationKey, AgentSessionManager
from arrest_report_agent.application.session_input import HISTORICAL_IMAGE_PLACEHOLDER
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


def _result(item):
    assert item["role"] == "user"
    assert "no instrucciones" in item["content"]
    assert not {"id", "call_id", "type"}.intersection(item)
    return json.loads(item["content"].split("\n", 1)[1])


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

    assert len(result) == 2
    assert result[0] == items[0][1]
    assert _result(result[1]) == {
        "tool": "get_character_legal_facts",
        "arguments": {"character_name": "Dústin_Vogel", "exclude_arrest_id": 8215},
        "output": "dos multas",
    }
    assert "dato privado" not in str(result)
    assert "prompt con contexto interno" not in str(result)
    assert "borrador" not in str(result)


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

    assert [_result(item) for item in _view(items)] == [
        {"tool": name, "arguments": json.loads(arguments), "output": name}
        for name, arguments, allowed in calls if allowed
    ]


def test_staff_reasoning_and_call_ids_are_not_replayed_but_owner_protocol_is_intact() -> None:
    owner_items = [
        {"role": "user", "content": "consulta anterior"},
        {"type": "reasoning", "id": "rs_owner", "summary": []},
        {"type": "function_call", "id": "fc_owner", "name": "get_report_guide",
         "call_id": "own-call", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "own-call", "output": "guía"},
        {"role": "assistant", "content": "respuesta anterior"},
    ]
    stored = [("owner", item) for item in owner_items] + [
        ("staff", {"type": "reasoning", "id": "rs_staff", "summary": [],
                   "encrypted_content": "private-encrypted-reasoning"}),
        ("staff", {"type": "function_call", "id": "fc_staff", "name": "get_current_arrest_report",
                   "call_id": "staff-call", "arguments": "{}"}),
        ("staff", {"type": "function_call_output", "call_id": "staff-call", "output": "reporte"}),
        ("owner", {"role": "user", "content": "consulta posterior"}),
    ]
    original = deepcopy(stored)

    visible = _view(stored)

    assert stored == original
    assert visible[:len(owner_items)] == owner_items
    assert visible[-1] == stored[-1][1]
    assert _result(visible[-2])["output"] == "reporte"
    for private in ("rs_staff", "fc_staff", "staff-call", "private-encrypted-reasoning"):
        assert private not in str(visible)


def test_only_complete_pairs_from_the_same_session_are_shared() -> None:
    stored = [
        ("staff", {"type": "function_call_output", "call_id": "orphan", "output": "huérfano"}),
        ("staff", {"type": "function_call", "name": "get_report_guide",
                   "call_id": "shared-id", "arguments": "{}"}),
        ("other-staff", {"type": "function_call", "name": "get_messages",
                         "call_id": "shared-id", "arguments": '{"channel_id":999}'}),
        ("other-staff", {"type": "function_call_output", "call_id": "shared-id", "output": "privado"}),
        ("staff", {"type": "function_call_output", "call_id": "shared-id", "output": "guía"}),
        ("staff", {"type": "function_call_output", "call_id": "shared-id", "output": "duplicado"}),
        ("staff", {"type": "function_call", "name": "get_report_guide",
                   "call_id": "no-output", "arguments": "{}"}),
        ("staff", {"type": "function_call", "name": "get_report_guide",
                   "call_id": "bad-json", "arguments": "not json"}),
        ("staff", {"type": "function_call_output", "call_id": "bad-json", "output": "inválido"}),
    ]

    visible = _view(stored)

    assert len(visible) == 1
    assert _result(visible[0])["output"] == "guía"


def test_shared_results_sanitize_multimodal_history_before_encoding() -> None:
    stored = [
        ("staff", {"type": "function_call", "name": "get_report_suspect_images",
                   "call_id": "images", "arguments": '{"report_id":8215}'}),
        ("staff", {"type": "function_call_output", "call_id": "images", "output": [
            {"type": "input_text", "text": "fotografía del sospechoso"},
            {"type": "input_image", "image_url": "data:image/png;base64,old-payload"},
        ]}),
    ]
    original = deepcopy(stored)

    visible = _view(stored)

    assert stored == original
    assert _result(visible[0])["output"] == [
        {"type": "input_text", "text": "fotografía del sospechoso"},
        {"type": "input_text", "text": HISTORICAL_IMAGE_PLACEHOLDER},
    ]
    assert "old-payload" not in str(visible)


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
            {"type": "reasoning", "id": "rs_staff", "summary": []},
            {
                "type": "function_call",
                "id": "fc_staff",
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
    visible = await resumed.session.get_items()
    assert len(visible) == 2
    assert visible[0] == {"role": "user", "content": "revisión inicial"}
    assert _result(visible[1])["output"] == "dos multas"
    assert "rs_staff" not in str(visible)
    assert "fc_staff" not in str(visible)
    # Reading another partition never rewrites the source or the owner's durable history.
    assert len(sessions[owner_key.session_id].items) == 1
    assert len(sessions[staff_key.session_id].items) == 3
