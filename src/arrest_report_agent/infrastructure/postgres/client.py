"""Lifecycle-safe async PostgreSQL client."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, TypeVar

import asyncpg
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.sql import Executable

from arrest_report_agent.config.logging import exception_fields, log_event

LOGGER = logging.getLogger(__name__)
T = TypeVar("T")


class DatabaseUnavailableError(RuntimeError):
    """Raised when PostgreSQL cannot serve a requested operation."""


@dataclass(frozen=True, slots=True)
class PostgresClientConfig:
    url: str
    pool_size: int = 3
    max_overflow: int = 2
    pool_timeout_seconds: float = 10.0
    pool_recycle_seconds: int = 900
    statement_timeout_ms: int = 15_000
    connect_attempts: int = 5
    connect_backoff_seconds: float = 0.5

    @property
    def async_url(self) -> str:
        value = self.url.strip()
        if value.startswith("postgres://"):
            value = "postgresql://" + value[len("postgres://") :]
        if value.startswith("postgresql://"):
            value = "postgresql+asyncpg://" + value[len("postgresql://") :]
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must be a PostgreSQL connection URL")
        return value

    @property
    def asyncpg_url(self) -> str:
        return self.async_url.replace("postgresql+asyncpg://", "postgresql://", 1)


class PostgresClient:
    """Own the SQLAlchemy pool and one reconnecting LISTEN connection."""

    def __init__(self, config: PostgresClientConfig) -> None:
        self.config = config
        self._engine: AsyncEngine | None = None
        self._listener: asyncpg.Connection[Any] | None = None
        self._listener_task: asyncio.Task[None] | None = None
        self._listener_stop = asyncio.Event()

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise DatabaseUnavailableError("PostgreSQL client is not open")
        return self._engine

    async def open(self) -> None:
        if self._engine is not None:
            return
        self._engine = create_async_engine(
            self.config.async_url,
            pool_pre_ping=True,
            pool_size=self.config.pool_size,
            max_overflow=self.config.max_overflow,
            pool_timeout=self.config.pool_timeout_seconds,
            pool_recycle=self.config.pool_recycle_seconds,
            connect_args={
                "server_settings": {
                    "statement_timeout": str(self.config.statement_timeout_ms),
                    "application_name": "arrest-report-agent",
                }
            },
        )
        last_error: Exception | None = None
        for attempt in range(1, self.config.connect_attempts + 1):
            try:
                await self.ping()
                log_event(LOGGER, "info", "postgres_connected", attempt=attempt)
                return
            except Exception as error:
                last_error = error
                if attempt == self.config.connect_attempts:
                    break
                await asyncio.sleep(self.config.connect_backoff_seconds * 2 ** (attempt - 1))
        await self.close()
        raise DatabaseUnavailableError("Unable to connect to PostgreSQL") from last_error

    async def close(self) -> None:
        self._listener_stop.set()
        if self._listener_task is not None:
            self._listener_task.cancel()
            await asyncio.gather(self._listener_task, return_exceptions=True)
            self._listener_task = None
        if self._listener is not None:
            await self._listener.close()
            self._listener = None
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
        log_event(LOGGER, "info", "postgres_closed")

    async def ping(self) -> bool:
        async with self.engine.connect() as connection:
            return bool((await connection.scalar(sa.text("SELECT 1"))) == 1)

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncConnection]:
        try:
            async with self.engine.connect() as connection:
                yield connection
        except (OSError, asyncpg.PostgresError, sa.exc.SQLAlchemyError) as error:
            raise DatabaseUnavailableError("PostgreSQL operation failed") from error

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncConnection]:
        """Open one transaction; never retry work that may have partially executed."""

        try:
            async with self.engine.begin() as connection:
                yield connection
        except (OSError, asyncpg.PostgresError, sa.exc.SQLAlchemyError) as error:
            raise DatabaseUnavailableError("PostgreSQL transaction failed") from error

    async def execute(
        self,
        statement: Executable,
        parameters: dict[str, Any] | None = None,
        *,
        connection: AsyncConnection | None = None,
    ) -> sa.engine.CursorResult[Any]:
        if connection is not None:
            return await connection.execute(statement, parameters or {})
        async with self.transaction() as transaction:
            return await transaction.execute(statement, parameters or {})

    async def fetch_one(
        self,
        statement: Executable,
        parameters: dict[str, Any] | None = None,
        *,
        connection: AsyncConnection | None = None,
    ) -> sa.RowMapping | None:
        if connection is not None:
            result = await connection.execute(statement, parameters or {})
            return result.mappings().one_or_none()
        async with self.connection() as read_connection:
            result = await read_connection.execute(statement, parameters or {})
            return result.mappings().one_or_none()

    async def fetch_all(
        self,
        statement: Executable,
        parameters: dict[str, Any] | None = None,
        *,
        connection: AsyncConnection | None = None,
    ) -> list[sa.RowMapping]:
        if connection is not None:
            result = await connection.execute(statement, parameters or {})
            return list(result.mappings().all())
        async with self.connection() as read_connection:
            result = await read_connection.execute(statement, parameters or {})
            return list(result.mappings().all())

    async def notify(self, channel: str, payload: str, connection: AsyncConnection) -> None:
        if not channel.replace("_", "").isalnum():
            raise ValueError("Unsafe PostgreSQL notification channel")
        await connection.execute(
            sa.text("SELECT pg_notify(:channel, :payload)"),
            {"channel": channel, "payload": payload},
        )

    def start_listener(
        self,
        channel: str,
        callback: Callable[[str], Awaitable[None]],
    ) -> None:
        if self._listener_task is not None:
            raise RuntimeError("PostgreSQL listener is already running")
        if not channel.replace("_", "").isalnum():
            raise ValueError("Unsafe PostgreSQL notification channel")
        self._listener_stop.clear()
        self._listener_task = asyncio.create_task(
            self._listen_loop(channel, callback),
            name=f"postgres-listen-{channel}",
        )

    async def _listen_loop(
        self,
        channel: str,
        callback: Callable[[str], Awaitable[None]],
    ) -> None:
        backoff = self.config.connect_backoff_seconds
        while not self._listener_stop.is_set():
            try:
                listener = await asyncpg.connect(
                    self.config.asyncpg_url,
                    server_settings={"application_name": "arrest-report-agent-listener"},
                )
                self._listener = listener

                def dispatch(
                    _connection: asyncpg.Connection[Any],
                    _pid: int,
                    _channel: str,
                    payload: str,
                ) -> None:
                    async def invoke() -> None:
                        await callback(payload)

                    asyncio.create_task(invoke())

                await listener.add_listener(channel, dispatch)
                log_event(LOGGER, "info", "postgres_listener_connected", channel=channel)
                backoff = self.config.connect_backoff_seconds
                while not self._listener_stop.is_set() and not listener.is_closed():
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log_event(
                    LOGGER,
                    "warning",
                    "postgres_listener_disconnected",
                    exc_info=error,
                    channel=channel,
                    **exception_fields(error),
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                if self._listener is not None:
                    await self._listener.close()
                    self._listener = None
