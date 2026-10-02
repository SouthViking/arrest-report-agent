"""Transport-independent coordination and live input for one active conversation."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol, cast, runtime_checkable

from agents.items import TResponseInputItem
from agents.memory import Session
from agents.run_config import CallModelData, ModelInputData

from arrest_report_agent.config.logging import log_event

LOGGER = logging.getLogger(__name__)
LIVE_MESSAGE_PREFIX = "[NUEVO_MENSAJE_DISCORD]\n"


@dataclass(frozen=True)
class ConversationKey:
    guild_id: int
    channel_id: int


@dataclass(frozen=True)
class IncomingMessage:
    id: int
    author_id: int
    text: str

    def input_item(self) -> TResponseInputItem:
        return {"role": "user", "content": LIVE_MESSAGE_PREFIX + self.text}


class ConversationStore(Protocol):
    """Atomic operations; implementations own concurrency and bounded queue storage.

    A lease may own aliases (a channel and a thread created during its run). Closing
    must test for pending messages and relinquish ownership in the same operation.
    """

    async def acquire(self, key: ConversationKey, trigger_id: int) -> str | None: ...
    async def enqueue(self, key: ConversationKey, message: IncomingMessage) -> str: ...
    async def add_alias(self, lease: str, key: ConversationKey) -> bool: ...
    async def discard_seen(self, lease: str, message_ids: set[int]) -> None: ...
    async def drain(self, lease: str) -> list[IncomingMessage]: ...
    async def has_pending(self, lease: str) -> bool: ...
    async def close_if_empty(self, lease: str) -> bool: ...
    async def release(self, lease: str) -> int: ...


@runtime_checkable
class SourceCursorSession(Protocol):
    def set_next_source_cursor(self, message_id: int) -> None: ...


class LiveConversation:
    """Merge a bounded inbox at model boundaries, without coupling it to Discord/Redis."""

    def __init__(self, store: ConversationStore, lease: str, key: ConversationKey) -> None:
        self.store = store
        self.lease = lease
        self.key = key
        self.model_calls = 0
        self.revision = 0
        self.output_revision = -1
        self.received: list[TResponseInputItem] = []
        self.message_ids: set[int] = set()
        self._insertions: list[tuple[int, list[TResponseInputItem]]] = []
        self._session: Session | None = None
        self._max_model_calls = 0

    def begin_pass(self, session: Session | None, max_model_calls: int) -> None:
        self._session = session
        self._max_model_calls = max_model_calls
        self._insertions = []

    async def filter_input(self, data: CallModelData[Any]) -> ModelInputData:
        if self.model_calls >= self._max_model_calls:
            raise RuntimeError("Live conversation model-call budget exhausted")
        self.model_calls += 1
        pending = await self.store.drain(self.lease)
        if pending:
            self.message_ids.update(message.id for message in pending)
            items = [message.input_item() for message in pending]
            self.received.extend(items)
            self._insertions.append((len(data.model_data.input), items))
            self.revision += 1
            # Input filters do not persist their additions. Previous tool results have
            # already been saved by the SDK, so append this batch before this model turn.
            if self._session is not None:
                if isinstance(self._session, SourceCursorSession):
                    self._session.set_next_source_cursor(max(message.id for message in pending))
                await self._session.add_items(items)
            log_event(
                LOGGER,
                "info",
                "conversation_messages_incorporated",
                guild_id=self.key.guild_id,
                channel_id=self.key.channel_id,
                lease_id=self.lease,
                count=len(items),
                revision=self.revision,
                last_message_id=max(message.id for message in pending),
            )
        return ModelInputData(
            instructions=data.model_data.instructions,
            input=self.merge_input(data.model_data.input),
        )

    def merge_input(self, items: list[TResponseInputItem]) -> list[TResponseInputItem]:
        """Keep injected turns in their original position on subsequent model calls."""
        existing = {live_message_id(item) for item in items} - {None}
        result: list[TResponseInputItem] = []
        offset = 0
        for position, batch in self._insertions:
            position = min(position, len(items))
            result.extend(items[offset:position])
            result.extend(item for item in batch if live_message_id(item) not in existing)
            existing.update(live_message_id(item) for item in batch)
            offset = position
        result.extend(items[offset:])
        return result

    async def defer_output(self) -> dict[str, object] | None:
        if not await self.store.has_pending(self.lease):
            return None
        log_event(
            LOGGER,
            "info",
            "conversation_output_deferred",
            guild_id=self.key.guild_id,
            channel_id=self.key.channel_id,
            lease_id=self.lease,
        )
        return {
            "ok": False,
            "status": "new_messages_pending",
            "message": "No se ejecutó la acción. Recibirás mensajes nuevos antes de tu siguiente paso; "
            "considéralos antes de volver a publicar.",
        }

    def note_output(self) -> None:
        self.output_revision = self.revision


def live_message_id(item: TResponseInputItem) -> int | None:
    if not isinstance(item, dict) or item.get("role") != "user":
        return None
    content = item.get("content")
    if not isinstance(content, str) or not content.startswith(LIVE_MESSAGE_PREFIX):
        return None
    try:
        payload = json.loads(content[len(LIVE_MESSAGE_PREFIX) :])
        return int(payload["message_id"])
    except (ValueError, KeyError, TypeError):
        return None


def with_received_messages(
    items: list[dict[str, Any]],
    received: list[TResponseInputItem],
) -> list[dict[str, Any]]:
    """Retain live messages if durable-session failure requires the existing fallback."""
    existing = {live_message_id(cast(TResponseInputItem, item)) for item in items}
    return [
        *items,
        *(cast(dict[str, Any], item) for item in received if live_message_id(item) not in existing),
    ]
