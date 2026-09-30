"""Small, reusable async scheduler; jobs own their durable business state."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from time import monotonic

from arrest_report_agent.config.logging import exception_fields, log_event

LOGGER = logging.getLogger(__name__)


@dataclass
class MaintenanceJob:
    callback: Callable[[], Awaitable[None]]
    interval_seconds: float
    next_run: float


class MaintenanceScheduler:
    """Check due jobs every minute without overlapping executions or busy polling.

    Deadlines are in-memory: after a restart jobs run once as soon as ready.
    Callbacks must be idempotent and persist any work that needs retries.
    """

    def __init__(
        self,
        ready: Callable[[], bool],
        *,
        tick_seconds: float = 60,
        job_timeout_seconds: float = 120,
    ) -> None:
        if tick_seconds <= 0 or job_timeout_seconds <= 0:
            raise ValueError("Maintenance intervals must be positive")
        self.ready = ready
        self.tick_seconds = tick_seconds
        self.job_timeout_seconds = job_timeout_seconds
        self._jobs: dict[str, MaintenanceJob] = {}
        self._task: asyncio.Task[None] | None = None
        self._processing = asyncio.Lock()
        self._stop = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def register(
        self, name: str, callback: Callable[[], Awaitable[None]], *, interval_seconds: float,
    ) -> None:
        if not name or interval_seconds <= 0:
            raise ValueError("Maintenance jobs require a name and positive interval")
        previous = self._jobs.get(name)
        deadline = (
            previous.next_run
            if previous is not None and previous.interval_seconds == interval_seconds
            else monotonic()
        )
        self._jobs[name] = MaintenanceJob(callback, interval_seconds, deadline)

    def unregister(self, name: str) -> None:
        self._jobs.pop(name, None)

    def start(self) -> None:
        if not self.running:
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="maintenance")

    async def close(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.process_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log_event(LOGGER, "error", "maintenance_cycle_failed", **exception_fields(error))
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.tick_seconds)

    async def process_once(self) -> None:
        if not self.ready() or self._processing.locked():
            return
        async with self._processing:
            for name, job in tuple(self._jobs.items()):
                if self._jobs.get(name) is not job or monotonic() < job.next_run:
                    continue
                try:
                    await asyncio.wait_for(job.callback(), timeout=self.job_timeout_seconds)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    log_event(
                        LOGGER, "error", "maintenance_job_failed", job=name, **exception_fields(error)
                    )
                finally:
                    # Configuration may replace this job while its callback is running.
                    job.next_run = monotonic() + job.interval_seconds
