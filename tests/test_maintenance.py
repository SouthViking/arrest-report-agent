from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from arrest_report_agent.application.maintenance import MaintenanceScheduler


async def test_scheduler_waits_for_readiness_and_respects_independent_intervals(monkeypatch, caplog):
    clock = [0.0]
    ready = [False]
    monkeypatch.setattr("arrest_report_agent.application.maintenance.monotonic", lambda: clock[0])
    scheduler = MaintenanceScheduler(lambda: ready[0])
    fast, slow = AsyncMock(), AsyncMock()
    scheduler.register("fast", fast, interval_seconds=60)
    scheduler.register("slow", slow, interval_seconds=600)
    await scheduler.process_once()
    fast.assert_not_awaited()
    ready[0] = True
    await scheduler.process_once()
    assert fast.await_count == slow.await_count == 1
    clock[0] = 59
    await scheduler.process_once()
    assert fast.await_count == slow.await_count == 1
    clock[0] = 60
    await scheduler.process_once()
    assert fast.await_count == 2 and slow.await_count == 1
    clock[0] = 600
    await scheduler.process_once()
    assert fast.await_count == 3 and slow.await_count == 2
    assert not caplog.records


async def test_scheduler_updates_intervals_and_unregisters_without_duplicate_jobs(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("arrest_report_agent.application.maintenance.monotonic", lambda: clock[0])
    scheduler = MaintenanceScheduler(lambda: True)
    callback = AsyncMock()
    scheduler.register("a", callback, interval_seconds=600)
    await scheduler.process_once()
    clock[0] = 10
    scheduler.register("a", callback, interval_seconds=600)
    await scheduler.process_once()
    assert callback.await_count == 1
    scheduler.register("a", callback, interval_seconds=60)
    await scheduler.process_once()
    assert callback.await_count == 2
    scheduler.unregister("a")
    clock[0] = 1000
    await scheduler.process_once()
    assert callback.await_count == 2


async def test_scheduler_isolates_failure_and_timeout(caplog):
    scheduler = MaintenanceScheduler(lambda: True, job_timeout_seconds=0.01)
    bad = AsyncMock(side_effect=RuntimeError("failure"))

    async def hung():
        await asyncio.Event().wait()

    good = AsyncMock()
    scheduler.register("bad", bad, interval_seconds=60)
    scheduler.register("hung", hung, interval_seconds=60)
    scheduler.register("good", good, interval_seconds=60)
    await scheduler.process_once()
    await scheduler.process_once()
    good.assert_awaited_once()
    bad.assert_awaited_once()
    assert sum("maintenance_job_failed" in record.message for record in caplog.records) == 2


async def test_scheduler_does_not_overlap_and_shutdown_cancels_inflight_work():
    started, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def callback():
        calls.append(1)
        started.set()
        try:
            await release.wait()
        finally:
            cancelled.set()

    scheduler = MaintenanceScheduler(lambda: True)
    scheduler.register("job", callback, interval_seconds=600)
    scheduler.start()
    scheduler.start()
    await asyncio.wait_for(started.wait(), 1)
    await scheduler.process_once()
    assert calls == [1]
    await scheduler.close()
    assert cancelled.is_set() and not scheduler.running
    await scheduler.close()


async def test_scheduler_waits_between_empty_ticks():
    checked = asyncio.Event()
    checks = []

    def ready():
        checks.append(1)
        checked.set()
        return True

    scheduler = MaintenanceScheduler(ready)
    scheduler.start()
    await asyncio.wait_for(checked.wait(), 1)
    await asyncio.sleep(0.01)
    assert checks == [1]
    await scheduler.close()


@pytest.mark.parametrize("interval", [0, -1])
def test_scheduler_rejects_busy_loop_intervals(interval):
    with pytest.raises(ValueError):
        MaintenanceScheduler(lambda: True, tick_seconds=interval)
    scheduler = MaintenanceScheduler(lambda: True)
    with pytest.raises(ValueError):
        scheduler.register("job", AsyncMock(), interval_seconds=interval)
