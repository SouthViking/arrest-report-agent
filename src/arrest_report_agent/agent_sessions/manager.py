"""Agents SDK session adapter with generic metadata and diagnostic logging."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from agents.extensions.memory import SQLAlchemySession
from agents.items import TResponseInputItem
from agents.memory import Session, SessionSettings
from openai.types.responses import ResponseInputItemParam

from arrest_report_agent.application.errors import AgentSessionUnavailableError
from arrest_report_agent.application.session_input import sanitize_session_items
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import PostgresClient

from .models import AgentConversationKey
from .repository import AgentSessionRepository

LOGGER = logging.getLogger(__name__)
SessionFactory = Callable[[str], Session]
SessionHistoryFilter = Callable[
    [list[tuple[str, TResponseInputItem]]],
    list[TResponseInputItem],
]


@dataclass(frozen=True, slots=True)
class PreparedAgentSession:
    session: Session
    source_cursor: str | None
    created: bool
    readable_source_cursors: tuple[str, ...] = ()


class _CursorTrackingSession:
    """Advance source metadata after the SDK has durably stored the new input."""

    def __init__(
        self,
        delegate: Session,
        repository: AgentSessionRepository,
        *,
        next_source_cursor: str,
        key: AgentConversationKey,
        readable_partitions: tuple[str, ...] = (),
        history_filter: SessionHistoryFilter | None = None,
    ) -> None:
        self._delegate = delegate
        self._repository = repository
        self._next_source_cursor = next_source_cursor
        self._key = key
        self._readable_partitions = readable_partitions
        self._history_filter = history_filter
        self._cursor_pending = True
        self.session_id = delegate.session_id
        self.session_settings: SessionSettings | None = delegate.session_settings

    async def get_items(self, limit: int | None = None) -> list[ResponseInputItemParam]:
        try:
            if self._readable_partitions:
                stored = await self._repository.read_items(
                    self._key,
                    readable_partitions=self._readable_partitions,
                )
                items = (
                    self._history_filter(stored)
                    if self._history_filter is not None
                    else [item for _, item in stored]
                )
                resolved_limit = limit if limit is not None else getattr(self.session_settings, "limit", None)
                if resolved_limit is not None:
                    return items[-resolved_limit:] if resolved_limit > 0 else []
                return items
            return await self._delegate.get_items(limit)
        except Exception as error:
            self._log_storage_failure("agent_session_read_failed", error)
            raise AgentSessionUnavailableError("Unable to read the agent session") from error

    async def add_items(self, items: list[ResponseInputItemParam]) -> None:
        try:
            await self._delegate.add_items(sanitize_session_items(items))
        except Exception as error:
            self._log_storage_failure("agent_session_write_failed", error)
            raise AgentSessionUnavailableError("Unable to write the agent session") from error
        if not items or not self._cursor_pending:
            return
        self._cursor_pending = False
        try:
            await self._repository.advance_source_cursor(
                self.session_id,
                self._next_source_cursor,
            )
        except Exception as error:
            # The SDK history is already durable. Do not fail the user interaction only because
            # the optimization cursor could not be updated; a later run can still reconstruct
            # the Discord context and the warning provides enough detail for diagnosis.
            log_event(
                LOGGER,
                "warning",
                "agent_session_cursor_update_failed",
                exc_info=error,
                session_id=self.session_id,
                namespace=self._key.namespace,
                access_partition=self._key.access_partition,
                **exception_fields(error),
            )

    async def pop_item(self) -> ResponseInputItemParam | None:
        try:
            return await self._delegate.pop_item()
        except Exception as error:
            self._log_storage_failure("agent_session_pop_failed", error)
            raise AgentSessionUnavailableError("Unable to rewind the agent session") from error

    async def clear_session(self) -> None:
        try:
            await self._delegate.clear_session()
        except Exception as error:
            self._log_storage_failure("agent_session_clear_failed", error)
            raise AgentSessionUnavailableError("Unable to clear the agent session") from error

    def _log_storage_failure(self, event: str, error: Exception) -> None:
        log_event(
            LOGGER,
            "error",
            event,
            exc_info=error,
            session_id=self.session_id,
            namespace=self._key.namespace,
            access_partition=self._key.access_partition,
            **exception_fields(error),
        )


class AgentSessionManager:
    """Prepare SDK sessions while keeping feature activation outside this mechanism."""

    def __init__(
        self,
        client: PostgresClient,
        repository: AgentSessionRepository | None = None,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self.repository = repository or AgentSessionRepository(client)
        self._session_factory = session_factory or (
            lambda session_id: SQLAlchemySession(
                session_id,
                engine=client.engine,
                create_tables=False,
                ensure_ascii=False,
            )
        )

    async def prepare(
        self,
        key: AgentConversationKey,
        *,
        next_source_cursor: str,
        readable_partitions: tuple[str, ...] = (),
        history_filter: SessionHistoryFilter | None = None,
        include_readable_cursors: bool = True,
    ) -> PreparedAgentSession:
        record = await self.repository.get_or_create(key)
        readable_partitions = tuple(
            partition for partition in dict.fromkeys(readable_partitions)
            if partition != key.access_partition
        )
        readable_cursors_list: list[str] = []
        if include_readable_cursors:
            for partition in readable_partitions:
                cursor = await self.repository.read_source_cursor(
                    AgentConversationKey(key.namespace, key.scope_key, partition)
                )
                if cursor is not None:
                    readable_cursors_list.append(cursor)
        delegate = self._session_factory(record.session_id)
        session = _CursorTrackingSession(
            delegate,
            self.repository,
            next_source_cursor=next_source_cursor,
            key=key,
            readable_partitions=readable_partitions,
            history_filter=history_filter,
        )
        log_event(
            LOGGER,
            "info" if record.created else "debug",
            "agent_session_created" if record.created else "agent_session_resumed",
            session_id=record.session_id,
            namespace=key.namespace,
            access_partition=key.access_partition,
            has_source_cursor=record.source_cursor is not None,
        )
        return PreparedAgentSession(
            session=session,
            source_cursor=record.source_cursor,
            created=record.created,
            readable_source_cursors=tuple(readable_cursors_list),
        )
