from __future__ import annotations

from typing import Any

import pytest

from arrest_report_agent.agent_sessions import (
    AgentConversationKey,
    AgentSessionManager,
    AgentSessionRecord,
)
from arrest_report_agent.application.session_input import (
    HISTORICAL_FILE_PLACEHOLDER,
    HISTORICAL_IMAGE_PLACEHOLDER,
    merge_sanitized_session_input,
)
from arrest_report_agent.config.logging import STRUCTURED_EVENT_ATTRIBUTE


class FakeSession:
    def __init__(
        self,
        session_id: str,
        history: list[tuple[str, dict[str, Any]]] | None = None,
    ) -> None:
        self.session_id = session_id
        self.session_settings = None
        self.items: list[dict[str, Any]] = []
        self.history = history

    async def get_items(self, limit: int | None = None) -> list[dict[str, Any]]:
        return self.items[-limit:] if limit is not None else list(self.items)

    async def add_items(self, items: list[dict[str, Any]]) -> None:
        self.items.extend(items)
        if self.history is not None:
            self.history.extend((self.session_id, item) for item in items)

    async def pop_item(self) -> dict[str, Any] | None:
        return self.items.pop() if self.items else None

    async def clear_session(self) -> None:
        self.items.clear()


class FakeRepository:
    def __init__(self, *, source_cursor: str | None = None, fail_advance: bool = False) -> None:
        self.source_cursor = source_cursor
        self.fail_advance = fail_advance
        self.prepared: list[AgentConversationKey] = []
        self.advanced: list[tuple[str, str]] = []
        self.cursors: dict[str, str] = {}
        self.history: list[tuple[str, dict[str, Any]]] = []

    async def get_or_create(self, key: AgentConversationKey) -> AgentSessionRecord:
        self.prepared.append(key)
        return AgentSessionRecord(
            session_id=key.session_id,
            source_cursor=self.cursors.get(key.session_id, self.source_cursor),
            created=key.session_id not in self.cursors and self.source_cursor is None,
        )

    async def advance_source_cursor(self, session_id: str, source_cursor: str) -> None:
        if self.fail_advance:
            raise RuntimeError("cursor unavailable")
        self.advanced.append((session_id, source_cursor))
        self.cursors[session_id] = source_cursor

    async def read_source_cursor(self, key: AgentConversationKey) -> str | None:
        return self.cursors.get(key.session_id)

    async def read_items(
        self,
        key: AgentConversationKey,
        *,
        readable_partitions: tuple[str, ...],
    ) -> list[tuple[str, dict[str, Any]]]:
        session_ids = {
            AgentConversationKey(key.namespace, key.scope_key, partition).session_id
            for partition in (key.access_partition, *readable_partitions)
        }
        return [(session_id, item) for session_id, item in self.history if session_id in session_ids]


def test_conversation_key_is_stable_and_access_partitioned() -> None:
    staff = AgentConversationKey("discord_agent_thread", "guild:1:root:2", "full")
    same = AgentConversationKey("discord_agent_thread", "guild:1:root:2", "full")
    owner = AgentConversationKey("discord_agent_thread", "guild:1:root:2", "report_owner")

    assert staff.session_id == same.session_id
    assert staff.session_id != owner.session_id
    assert staff.session_id.startswith("agent-")
    assert "guild:1" not in staff.session_id


@pytest.mark.asyncio
async def test_manager_reuses_cursor_and_advances_it_after_first_durable_input() -> None:
    repository = FakeRepository(source_cursor="100")
    delegates: list[FakeSession] = []

    def session_factory(session_id: str) -> FakeSession:
        session = FakeSession(session_id)
        delegates.append(session)
        return session

    manager = AgentSessionManager(  # type: ignore[arg-type]
        object(),
        repository=repository,  # type: ignore[arg-type]
        session_factory=session_factory,  # type: ignore[arg-type]
    )
    key = AgentConversationKey("discord_agent_thread", "guild:1:root:2", "full")

    prepared = await manager.prepare(key, next_source_cursor="200")

    assert prepared.source_cursor == "100"
    assert not prepared.created
    assert repository.advanced == []

    first_input = [{"role": "user", "content": "nuevo"}]
    await prepared.session.add_items(first_input)  # type: ignore[arg-type]
    await prepared.session.add_items([{"type": "function_call_output", "output": "ok"}])  # type: ignore[arg-type]

    assert delegates[0].items == first_input + [{"type": "function_call_output", "output": "ok"}]
    assert repository.advanced == [(key.session_id, "200")]

    prepared.session.set_next_source_cursor(250)
    await prepared.session.add_items([{"role": "user", "content": "mensaje en vivo"}])
    prepared.session.set_next_source_cursor(220)
    await prepared.session.add_items([{"role": "assistant", "content": "respuesta"}])
    assert repository.advanced == [(key.session_id, "200"), (key.session_id, "250")]


@pytest.mark.asyncio
async def test_cursor_failure_is_logged_without_breaking_the_agent_run(caplog) -> None:
    repository = FakeRepository(fail_advance=True)
    manager = AgentSessionManager(  # type: ignore[arg-type]
        object(),
        repository=repository,  # type: ignore[arg-type]
        session_factory=FakeSession,  # type: ignore[arg-type]
    )
    key = AgentConversationKey("discord_agent_thread", "guild:1:root:2", "full")
    prepared = await manager.prepare(key, next_source_cursor="200")

    with caplog.at_level("WARNING", logger="arrest_report_agent.agent_sessions.manager"):
        await prepared.session.add_items([{"role": "user", "content": "nuevo"}])  # type: ignore[arg-type]

    events = [getattr(record, STRUCTURED_EVENT_ATTRIBUTE, {}).get("event") for record in caplog.records]
    assert "agent_session_cursor_update_failed" in events


@pytest.mark.asyncio
async def test_session_persistence_replaces_transient_media_but_keeps_tool_structure() -> None:
    repository = FakeRepository()
    delegates: list[FakeSession] = []

    def session_factory(session_id: str) -> FakeSession:
        session = FakeSession(session_id)
        delegates.append(session)
        return session

    manager = AgentSessionManager(  # type: ignore[arg-type]
        object(),
        repository=repository,  # type: ignore[arg-type]
        session_factory=session_factory,  # type: ignore[arg-type]
    )
    prepared = await manager.prepare(
        AgentConversationKey("discord_agent_thread", "guild:1:root:2", "full"),
        next_source_cursor="200",
    )
    items = [
        {
            "type": "function_call_output",
            "call_id": "call-images",
            "output": [
                {"type": "input_text", "text": '{"message_id":"123"}'},
                {
                    "type": "input_image",
                    "image_url": "https://cdn.discordapp.com/expired.png?token=old",
                },
                {
                    "type": "input_file",
                    "file_url": "https://cdn.discordapp.com/expired.pdf?token=old",
                },
            ],
        }
    ]

    await prepared.session.add_items(items)  # type: ignore[arg-type]

    stored = delegates[0].items[0]
    assert stored["type"] == "function_call_output"
    assert stored["call_id"] == "call-images"
    assert stored["output"] == [
        {"type": "input_text", "text": '{"message_id":"123"}'},
        {"type": "input_text", "text": HISTORICAL_IMAGE_PLACEHOLDER},
        {"type": "input_text", "text": HISTORICAL_FILE_PLACEHOLDER},
    ]
    assert "discordapp.com" not in str(stored)


def test_session_input_callback_sanitizes_history_but_keeps_current_media() -> None:
    historical = [
        {
            "role": "user",
            "content": [{"type": "input_image", "image_url": "https://cdn.discordapp.com/old.png"}],
        }
    ]
    current = [
        {
            "role": "user",
            "content": [{"type": "input_image", "image_url": "https://cdn.discordapp.com/current.png"}],
        }
    ]

    merged = merge_sanitized_session_input(historical, current)  # type: ignore[arg-type]

    assert merged[0]["content"] == [{"type": "input_text", "text": HISTORICAL_IMAGE_PLACEHOLDER}]
    assert merged[1] == current[0]


@pytest.mark.asyncio
async def test_staff_sees_owner_tool_history_in_order_without_leaking_staff_history_back() -> None:
    repository = FakeRepository()
    sessions: dict[str, FakeSession] = {}

    def session_factory(session_id: str) -> FakeSession:
        session = sessions.setdefault(session_id, FakeSession(session_id, repository.history))
        return session

    manager = AgentSessionManager(  # type: ignore[arg-type]
        object(),
        repository=repository,  # type: ignore[arg-type]
        session_factory=session_factory,  # type: ignore[arg-type]
    )
    owner_key = AgentConversationKey("discord_agent_thread", "guild:1:root:2", "report_owner")
    staff_key = AgentConversationKey("discord_agent_thread", "guild:1:root:2", "full")
    owner = await manager.prepare(owner_key, next_source_cursor="100")
    owner_turn = [
        {"role": "user", "content": "Revisa el reporte"},
        {"type": "function_call", "name": "get_character_legal_facts", "call_id": "facts-1"},
        {
            "type": "function_call_output",
            "call_id": "facts-1",
            "output": '{"matches":[{"description":"Exceso de velocidad"}]}',
        },
        {"role": "assistant", "content": "Hay dos multas impagas."},
    ]
    await owner.session.add_items(owner_turn)  # type: ignore[arg-type]

    staff = await manager.prepare(
        staff_key,
        next_source_cursor="200",
        readable_partitions=("report_owner",),
    )
    assert staff.readable_source_cursors == ("100",)
    assert await staff.session.get_items() == owner_turn

    private_turn = [
        {"role": "user", "content": "¿Por qué eran las multas?"},
        {"type": "function_call", "name": "private_staff_tool", "call_id": "staff-1"},
        {"type": "function_call_output", "call_id": "staff-1", "output": "privado"},
    ]
    await staff.session.add_items(private_turn)  # type: ignore[arg-type]
    await owner.session.add_items([{"role": "user", "content": "Otra corrección"}])  # type: ignore[arg-type]

    assert await staff.session.get_items() == [
        *owner_turn,
        *private_turn,
        {"role": "user", "content": "Otra corrección"},
    ]
    assert await staff.session.get_items(limit=2) == [
        private_turn[-1],
        {"role": "user", "content": "Otra corrección"},
    ]
    assert await owner.session.get_items() == [
        *owner_turn,
        {"role": "user", "content": "Otra corrección"},
    ]
