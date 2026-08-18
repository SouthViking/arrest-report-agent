from __future__ import annotations

from typing import Any

import discord
import pytest

from arrest_report_agent.domain import AgentActivity
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
async def test_presence_uses_global_priority_across_concurrent_interactions() -> None:
    bot = FakeBot()
    presence = ReviewPresence(
        bot,  # type: ignore[arg-type]
        idle_text="⭐ De guardia",
        conversation_text="📻 Atendiendo consultas",
        knowledge_text="📚 Consultando manual y códigos",
        personnel_text="👥 Consultando personal",
        records_text="🗃️ Consultando registros",
        evidence_text="🖼️ Analizando evidencias",
        review_text="📂 Revisando expedientes",
        response_text="✍️ Preparando respuesta",
    )

    await presence.synchronize()
    assert presence.mode is PresenceMode.IDLE

    async with presence.handling() as first:
        assert presence.active_requests == 1
        assert presence.mode is PresenceMode.CONVERSATION
        await first.transition(AgentActivity.PERSONNEL)
        assert presence.mode is PresenceMode.PERSONNEL
        await first.transition(AgentActivity.KNOWLEDGE)
        assert presence.mode is PresenceMode.KNOWLEDGE
        async with presence.handling() as second:
            assert presence.active_requests == 2
            assert presence.mode is PresenceMode.KNOWLEDGE
            await second.transition(AgentActivity.REVIEW)
            assert presence.mode is PresenceMode.REVIEW
            await first.transition(AgentActivity.RECORDS)
            assert presence.mode is PresenceMode.REVIEW
            await second.transition(AgentActivity.EVIDENCE)
            assert presence.mode is PresenceMode.EVIDENCE
        assert presence.mode is PresenceMode.RECORDS
        await first.transition(AgentActivity.RESPONSE)
        assert presence.mode is PresenceMode.RESPONSE

    assert presence.active_requests == 0
    assert presence.mode is PresenceMode.IDLE
    assert [activity_text(update) for update in bot.updates] == [
        "⭐ De guardia",
        "📻 Atendiendo consultas",
        "👥 Consultando personal",
        "📚 Consultando manual y códigos",
        "📂 Revisando expedientes",
        "🖼️ Analizando evidencias",
        "🗃️ Consultando registros",
        "✍️ Preparando respuesta",
        "⭐ De guardia",
    ]
    assert all(update["status"] is discord.Status.online for update in bot.updates)


@pytest.mark.asyncio
async def test_presence_failure_never_blocks_or_leaks_active_review_count() -> None:
    bot = FakeBot(error=RuntimeError("Gateway unavailable"))
    presence = ReviewPresence(
        bot,  # type: ignore[arg-type]
        idle_text="⭐ De guardia",
        conversation_text="📻 Atendiendo consultas",
        knowledge_text="📚 Consultando manual y códigos",
        personnel_text="👥 Consultando personal",
        records_text="🗃️ Consultando registros",
        evidence_text="🖼️ Analizando evidencias",
        review_text="📂 Revisando expedientes",
        response_text="✍️ Preparando respuesta",
    )

    await presence.synchronize()
    async with presence.handling() as activity:
        assert presence.active_requests == 1
        await activity.transition(AgentActivity.KNOWLEDGE)

    assert presence.active_requests == 0
    assert len(bot.updates) == 4
