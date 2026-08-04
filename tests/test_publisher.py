from __future__ import annotations

from types import SimpleNamespace

import pytest

from arrest_report_agent.domain import FindingSeverity, ReviewFinding, ReviewPublication
from arrest_report_agent.infrastructure.discord.context import (
    DiscordResponseRoute,
    ReviewTarget,
)
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
    def __init__(self, thread=None, guild=None) -> None:
        self.id = 42
        self.content = "Terry Parker <@999>"
        self.thread = thread
        self.guild = guild
        self.added = []
        self.removed = []
        self.created = 0
        self.replies = []
        self.channel = SimpleNamespace(sent=[])

        async def send(**kwargs):
            self.channel.sent.append(kwargs)

        self.channel.send = send

    async def add_reaction(self, emoji):
        self.added.append(emoji)

    async def remove_reaction(self, emoji, user):
        self.removed.append((emoji, user.id))

    async def create_thread(self, **kwargs):
        self.created += 1
        self.thread = FakeThread(owner_id=999)
        return self.thread

    async def reply(self, **kwargs):
        self.replies.append(kwargs)


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

    charge_embeds = [embed for embed in embeds if embed.title.startswith("⚖️")]

    assert len(embeds) == 4
    assert len(charge_embeds) == 2
    assert all(len(embed.fields) == 5 for embed in charge_embeds)
    assert all(field.inline is False for embed in charge_embeds for field in embed.fields)
    assert {field.name for embed in charge_embeds for field in embed.fields} == {
        f"Cargo {index}" for index in range(10)
    }
    assert all(len(embed) <= 6000 for embed in embeds)


def test_charge_alignments_are_rendered_as_user_facing_spanish_labels(settings) -> None:
    review = ReviewPublication(
        verdict="inconclusive",
        summary="Se revisaron los cargos declarados.",
        findings=[],
        charges=[
            {
                "declared_charge": f"Cargo {alignment}",
                "alignment": alignment,
                "note": "Resultado de la comprobación.",
            }
            for alignment in ("supported", "unsupported", "uncertain", "not_found")
        ],
        next_step="Confirmar la información pendiente.",
    )

    embeds = build_review_embeds(review, settings)
    charge_embed = next(embed for embed in embeds if embed.title.startswith("⚖️"))
    rendered = "\n".join(field.value for field in charge_embed.fields)

    assert [field.name for field in charge_embed.fields] == [
        "Cargo supported",
        "Cargo unsupported",
        "Cargo uncertain",
        "Cargo not_found",
    ]
    assert all(field.inline is False for field in charge_embed.fields)
    assert "✅ Sustentado" in rendered
    assert "❌ No sustentado" in rendered
    assert "⚠️ Requiere confirmación" in rendered
    assert "🔎 No encontrado en el catálogo" in rendered
    assert not any(
        f"— **{alignment}:" in rendered
        for alignment in (
            "supported",
            "unsupported",
            "uncertain",
            "not_found",
        )
    )


@pytest.mark.asyncio
async def test_initial_mention_approval_uses_and_archives_thread(settings) -> None:
    root = FakeRoot()
    bot = SimpleNamespace(user=SimpleNamespace(id=999))
    target = ReviewTarget(
        root=root,
        trigger=root,
        parent=SimpleNamespace(),
        thread=None,
        response_route=DiscordResponseRoute.THREAD,
    )
    publisher = DiscordReviewPublisher(bot, settings, target)

    await publisher.mark_review_started()
    await publisher.publish(publication("approved"))

    assert root.added == ["⏳", "✅"]
    assert root.created == 1
    assert root.thread.sent[0]["embeds"][0].title.startswith("✅")
    assert root.thread.edits[-1] == {"archived": True, "locked": True}
    assert all(user_id == 999 for _, user_id in root.removed)


@pytest.mark.asyncio
async def test_correction_creates_thread_and_posts_embed(settings) -> None:
    root = FakeRoot()
    bot = SimpleNamespace(user=SimpleNamespace(id=999))
    target = ReviewTarget(
        root=root,
        trigger=root,
        parent=SimpleNamespace(),
        thread=None,
        response_route=DiscordResponseRoute.THREAD,
    )
    publisher = DiscordReviewPublisher(bot, settings, target)

    await publisher.publish(publication("corrections_required"))

    assert root.added == ["❌"]
    assert root.created == 1
    assert root.thread.sent[0]["embeds"][0].title.startswith("❌")


@pytest.mark.asyncio
async def test_failure_publishes_only_contextual_message_and_next_step(settings) -> None:
    root = FakeRoot()
    bot = SimpleNamespace(user=SimpleNamespace(id=999))
    target = ReviewTarget(
        root=root,
        trigger=root,
        parent=SimpleNamespace(),
        thread=None,
        response_route=DiscordResponseRoute.THREAD,
    )
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


@pytest.mark.asyncio
async def test_failure_mentions_only_existing_configured_roles_once(settings) -> None:
    roles = {
        111: SimpleNamespace(id=111, mention="<@&111>"),
        333: SimpleNamespace(id=333, mention="<@&333>"),
    }
    guild = SimpleNamespace(id=10, get_role=roles.get)
    settings.review_escalation_role_ids_csv = "111,222,333,111"
    root = FakeRoot(guild=guild)
    bot = SimpleNamespace(user=SimpleNamespace(id=999))
    target = ReviewTarget(
        root=root,
        trigger=root,
        parent=SimpleNamespace(),
        thread=None,
        response_route=DiscordResponseRoute.THREAD,
    )
    publisher = DiscordReviewPublisher(bot, settings, target)

    await publisher.publish_failure("No pude completar la revisión con seguridad.")
    await publisher.publish_failure("El segundo aviso no debe repetir la derivación.")

    first, second = root.thread.sent
    assert first["content"] == "📣 Se requiere supervisión: <@&111> <@&333>"
    assert first["allowed_mentions"].to_dict() == {
        "roles": [111, 333],
        "parse": [],
    }
    assert second["content"] is None
    assert second["allowed_mentions"].to_dict() == {"parse": []}


@pytest.mark.asyncio
async def test_inconclusive_review_uses_the_same_escalation_roles(settings) -> None:
    role = SimpleNamespace(id=111, mention="<@&111>")
    guild = SimpleNamespace(id=10, get_role=lambda role_id: role if role_id == 111 else None)
    settings.review_escalation_role_ids_csv = "111"
    root = FakeRoot(guild=guild)
    bot = SimpleNamespace(user=SimpleNamespace(id=999))
    target = ReviewTarget(
        root=root,
        trigger=root,
        parent=SimpleNamespace(),
        thread=None,
        response_route=DiscordResponseRoute.THREAD,
    )
    publisher = DiscordReviewPublisher(bot, settings, target)

    await publisher.publish(publication("inconclusive"))

    sent = root.thread.sent[0]
    assert root.added == ["⚠️"]
    assert sent["content"] == "📣 Se requiere supervisión: <@&111>"
    assert sent["allowed_mentions"].to_dict() == {"roles": [111], "parse": []}


@pytest.mark.asyncio
async def test_natural_reply_is_simple_and_does_not_create_review_state(settings) -> None:
    root = FakeRoot()
    bot = SimpleNamespace(user=SimpleNamespace(id=999))
    target = ReviewTarget(root=root, trigger=root, parent=SimpleNamespace(), thread=None)
    publisher = DiscordReviewPublisher(bot, settings, target)

    await publisher.send_message("A la orden, deputy. ¿Qué punto del manual necesita revisar?")

    assert root.created == 0
    assert root.added == []
    assert root.replies[0]["content"].startswith("A la orden")
    assert root.replies[0]["mention_author"] is False


@pytest.mark.asyncio
async def test_mention_conversation_reuses_existing_thread(settings) -> None:
    thread = FakeThread(owner_id=123)
    root = FakeRoot(thread=thread)
    bot = SimpleNamespace(user=SimpleNamespace(id=999))
    target = ReviewTarget(
        root=root,
        trigger=root,
        parent=SimpleNamespace(),
        thread=thread,
        response_route=DiscordResponseRoute.THREAD,
    )
    publisher = DiscordReviewPublisher(bot, settings, target)

    await publisher.send_message("A la orden, deputy.")

    assert root.created == 0
    assert root.replies == []
    assert thread.sent[0]["content"] == "A la orden, deputy."


@pytest.mark.asyncio
async def test_inline_reply_review_never_creates_thread(settings) -> None:
    root = FakeRoot()
    bot = SimpleNamespace(user=SimpleNamespace(id=999))
    target = ReviewTarget(
        root=root,
        trigger=root,
        parent=SimpleNamespace(),
        thread=None,
        response_route=DiscordResponseRoute.INLINE_REPLY,
    )
    publisher = DiscordReviewPublisher(bot, settings, target)

    await publisher.publish(publication("corrections_required"))

    assert root.created == 0
    assert root.replies[0]["embeds"][0].title.startswith("❌")
