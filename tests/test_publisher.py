from __future__ import annotations

from types import SimpleNamespace

import pytest

from arrest_report_agent.domain import FindingSeverity, ReviewFinding, ReviewPublication
from arrest_report_agent.infrastructure.discord.context import ReviewTarget
from arrest_report_agent.infrastructure.discord.publisher import (
    DiscordReviewPublisher,
    build_review_embed,
)


class FakeThread:
    def __init__(self, owner_id: int) -> None:
        self.owner_id = owner_id
        self.archived = False
        self.sent = []
        self.edits = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        self.archived = kwargs.get("archived", self.archived)


class FakeRoot:
    def __init__(self, thread=None) -> None:
        self.id = 42
        self.content = "Terry Parker <@999>"
        self.thread = thread
        self.added = []
        self.removed = []
        self.created = 0

    async def add_reaction(self, emoji):
        self.added.append(emoji)

    async def remove_reaction(self, emoji, user):
        self.removed.append((emoji, user.id))

    async def create_thread(self, **kwargs):
        self.created += 1
        self.thread = FakeThread(owner_id=999)
        return self.thread


def publication(verdict: str) -> ReviewPublication:
    return ReviewPublication(
        verdict=verdict,
        summary="Resultado claro y fundamentado.",
        findings=(
            [
                ReviewFinding(
                    severity=FindingSeverity.REQUIRED,
                    title="Cronología",
                    detail="La hora no coincide con la secuencia narrada.",
                    recommendation="Aclarar la hora aproximada del inicio.",
                    evidence="Narrativa principal",
                )
            ]
            if verdict != "approved"
            else []
        ),
        charges=[],
        next_step="Aplicar las correcciones y solicitar una nueva revisión.",
    )


def test_embed_is_structured_and_uses_runtime_identity(settings) -> None:
    embed = build_review_embed(publication("corrections_required"), settings)

    assert settings.identity.display_name in embed.title
    assert embed.fields[0].name.endswith("Cronología")
    assert "Cómo corregirlo" in embed.fields[0].value


@pytest.mark.asyncio
async def test_initial_approval_adds_ticket_without_thread(settings) -> None:
    root = FakeRoot()
    bot = SimpleNamespace(user=SimpleNamespace(id=999))
    target = ReviewTarget(root=root, trigger=root, parent=SimpleNamespace(), thread=None)
    publisher = DiscordReviewPublisher(bot, settings, target)

    await publisher.begin()
    await publisher.publish(publication("approved"))

    assert root.added == ["⏳", "✅"]
    assert root.created == 0
    assert all(user_id == 999 for _, user_id in root.removed)


@pytest.mark.asyncio
async def test_correction_creates_thread_and_posts_embed(settings) -> None:
    root = FakeRoot()
    bot = SimpleNamespace(user=SimpleNamespace(id=999))
    target = ReviewTarget(root=root, trigger=root, parent=SimpleNamespace(), thread=None)
    publisher = DiscordReviewPublisher(bot, settings, target)

    await publisher.publish(publication("corrections_required"))

    assert root.added == ["❌"]
    assert root.created == 1
    assert root.thread.sent[0]["embed"].title.startswith("❌")

