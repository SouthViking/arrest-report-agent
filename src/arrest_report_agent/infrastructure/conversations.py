"""Bounded single-process implementation of the conversation storage port."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from arrest_report_agent.application.conversations import ConversationKey, IncomingMessage


@dataclass
class _Inbox:
    keys: set[ConversationKey]
    seen: set[int]
    pending: list[IncomingMessage] = field(default_factory=list)
    received: int = 0


class InMemoryConversationStore:
    """Async API with no suspension within each operation: atomic on one event loop.

    The bound covers the entire lease, not only pending messages, so the injected
    context and deduplication set cannot grow indefinitely either.
    """

    def __init__(self, max_messages: int = 100) -> None:
        self.max_messages = max_messages
        self._keys: dict[ConversationKey, str] = {}
        self._inboxes: dict[str, _Inbox] = {}

    async def acquire(self, key: ConversationKey, trigger_id: int) -> str | None:
        if key in self._keys:
            return None
        lease = uuid4().hex
        self._keys[key] = lease
        self._inboxes[lease] = _Inbox(keys={key}, seen={trigger_id})
        return lease

    async def enqueue(self, key: ConversationKey, message: IncomingMessage) -> str:
        lease = self._keys.get(key)
        if lease is None:
            return "inactive"
        inbox = self._inboxes[lease]
        if message.id in inbox.seen:
            return "duplicate"
        if inbox.received >= self.max_messages:
            return "full"
        inbox.seen.add(message.id)
        inbox.pending.append(message)
        inbox.received += 1
        return "enqueued"

    async def add_alias(self, lease: str, key: ConversationKey) -> bool:
        if lease not in self._inboxes:
            return False
        if key in self._keys and self._keys[key] != lease:
            return False
        self._keys[key] = lease
        self._inboxes[lease].keys.add(key)
        return True

    async def discard_seen(self, lease: str, message_ids: set[int]) -> None:
        inbox = self._inboxes[lease]
        inbox.pending = [message for message in inbox.pending if message.id not in message_ids]
        inbox.seen.update(message_ids)

    async def drain(self, lease: str) -> list[IncomingMessage]:
        inbox = self._inboxes[lease]
        pending, inbox.pending = inbox.pending, []
        return sorted(pending, key=lambda message: message.id)

    async def has_pending(self, lease: str) -> bool:
        inbox = self._inboxes.get(lease)
        return bool(inbox and inbox.pending)

    async def close_if_empty(self, lease: str) -> bool:
        if await self.has_pending(lease):
            return False
        await self.release(lease)
        return True

    async def release(self, lease: str) -> int:
        inbox = self._inboxes.pop(lease, None)
        if inbox is None:
            return 0
        for key in inbox.keys:
            self._keys.pop(key, None)
        return len(inbox.pending)
