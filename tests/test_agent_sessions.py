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
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.session_settings = None
        self.items: list[dict[str, Any]] = []

    async def get_items(self, limit: int | None = None) -> list[dict[str, Any]]:
        return self.items[-limit:] if limit is not None else list(self.items)

    async def add_items(self, items: list[dict[str, Any]]) -> None:
        self.items.extend(items)

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

    async def get_or_create(self, key: AgentConversationKey) -> AgentSessionRecord:
        self.prepared.append(key)
        return AgentSessionRecord(
            session_id=key.session_id,
            source_cursor=self.source_cursor,
            created=self.source_cursor is None,
        )

    async def advance_source_cursor(self, session_id: str, source_cursor: str) -> None:
        if self.fail_advance:
            raise RuntimeError("cursor unavailable")
        self.advanced.append((session_id, source_cursor))


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
