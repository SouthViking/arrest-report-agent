from __future__ import annotations

from typing import Any

import discord
import pytest

from arrest_report_agent.infrastructure.discord.presence import PresenceMode, ReviewPresence


class FakeBot:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.updates: list[dict[str, Any]] = []

    async def change_presence(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)
        if self.error is not None:
            raise self.error


def activity_text(update: dict[str, Any]) -> str | None:
    activity = update["activity"]
    assert isinstance(activity, discord.CustomActivity)
    return activity.name


@pytest.mark.asyncio
async def test_presence_changes_only_on_global_review_transitions() -> None:
    bot = FakeBot()
    presence = ReviewPresence(
        bot,  # type: ignore[arg-type]
        idle_text="⭐ De guardia",
        busy_text="📂 Revisando expedientes",
    )

    await presence.synchronize()
    assert presence.mode is PresenceMode.IDLE

    async with presence.reviewing():
        assert presence.active_reviews == 1
        assert presence.mode is PresenceMode.BUSY
        async with presence.reviewing():
            assert presence.active_reviews == 2
        assert presence.active_reviews == 1

    assert presence.active_reviews == 0
    assert presence.mode is PresenceMode.IDLE
    assert [activity_text(update) for update in bot.updates] == [
        "⭐ De guardia",
        "📂 Revisando expedientes",
        "⭐ De guardia",
    ]
    assert all(update["status"] is discord.Status.online for update in bot.updates)


@pytest.mark.asyncio
async def test_presence_failure_never_blocks_or_leaks_active_review_count() -> None:
    bot = FakeBot(error=RuntimeError("Gateway unavailable"))
    presence = ReviewPresence(
        bot,  # type: ignore[arg-type]
        idle_text="⭐ De guardia",
        busy_text="📂 Revisando expedientes",
    )

    await presence.synchronize()
    async with presence.reviewing():
        assert presence.active_reviews == 1

    assert presence.active_reviews == 0
    assert len(bot.updates) == 3
