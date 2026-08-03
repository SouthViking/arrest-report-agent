from __future__ import annotations

from types import SimpleNamespace

import pytest

from arrest_report_agent.domain import FindingSeverity, ReviewFinding, ReviewPublication
from arrest_report_agent.infrastructure.discord.context import ReviewTarget
from arrest_report_agent.infrastructure.discord.publisher import (
    DiscordReviewPublisher,
    build_review_embed,
    build_review_embeds,
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


def test_long_review_is_split_without_dropping_findings(settings) -> None:
    findings = [
        ReviewFinding(
            severity=FindingSeverity.REQUIRED,
            title=f"Corrección {index}",
            detail="Detalle material " * 20,
            recommendation="Corregir el punto indicado.",
        )
        for index in range(12)
    ]
    review = ReviewPublication(
        verdict="corrections_required",
        summary="Se requieren varias correcciones.",
        findings=findings,
        charges=[],
        next_step="Corregir y solicitar una nueva revisión.",
    )

    embeds = build_review_embeds(review, settings)
    rendered_names = [field.name for embed in embeds for field in embed.fields]

    assert len(embeds) == 4
    assert all(any(f"Corrección {index}" in name for name in rendered_names) for index in range(12))
    assert all(len(embed) <= 6000 for embed in embeds)


def test_long_charge_review_stays_within_discord_embed_limits(settings) -> None:
    review = ReviewPublication(
        verdict="corrections_required",
        summary="Los cargos requieren ajustes.",
        findings=[],
        charges=[
            {
                "charge_id": index,
                "declared_charge": f"Cargo {index}",
                "alignment": "unsupported",
                "note": "La narrativa no acredita los elementos descritos. " * 8,
            }
            for index in range(10)
        ],
        next_step="Revisar los cargos y corregir el expediente.",
    )

    embeds = build_review_embeds(review, settings)

    assert len(embeds) <= 10
    assert all(len(embed) <= 6000 for embed in embeds)


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
    assert root.thread.sent[0]["embeds"][0].title.startswith("❌")


@pytest.mark.asyncio
async def test_failure_publishes_only_contextual_message_and_next_step(settings) -> None:
    root = FakeRoot()
    bot = SimpleNamespace(user=SimpleNamespace(id=999))
    target = ReviewTarget(root=root, trigger=root, parent=SimpleNamespace(), thread=None)
    publisher = DiscordReviewPublisher(bot, settings, target)

    public_message = "La mesa de revisión alcanzó su capacidad operativa para este turno."
    await publisher.publish_failure(public_message)

    embed = root.thread.sent[0]["embed"]
    assert root.added == ["⚠️"]
    assert embed.title == "⚠️ Incidencia de revisión"
    assert embed.description == public_message
    assert [field.name for field in embed.fields] == ["Siguiente paso"]
    rendered = str(embed.to_dict())
    assert "Clasificación" not in rendered
    assert "Referencia técnica" not in rendered
    assert "informa" not in rendered
