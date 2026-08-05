"""Opt-in behavioral evaluations against the real OpenAI agent.

These tests deliberately omit Discord transport and use an in-memory publisher. They still run the
production prompt, scope guardrail, local knowledge tools, terminal tools and configured model.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from agents import set_default_openai_key
from pydantic import ValidationError

from arrest_report_agent.application import ReviewRunContext, ReviewService
from arrest_report_agent.config import AppSettings, ModelCatalog, PackagedPersonaProvider
from arrest_report_agent.domain import (
    ConversationMessage,
    InteractionMode,
    ReviewCase,
    ReviewPublication,
    ReviewVerdict,
)
from arrest_report_agent.infrastructure.discord.context import render_agent_input
from arrest_report_agent.knowledge import ChargeCatalog, ReportGuide

pytestmark = [pytest.mark.ai_eval, pytest.mark.asyncio(loop_scope="module")]

_REQUESTER_ID = 1001
_BOT_ID = 2001
_BASE_TIME = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


class InMemoryPublisher:
    """Capture the agent's terminal behavior without connecting to Discord."""

    def __init__(self) -> None:
        self.review_started = 0
        self.publications: list[ReviewPublication] = []
        self.messages: list[str] = []
        self.failures: list[str] = []

    async def mark_review_started(self) -> None:
        self.review_started += 1

    async def publish(self, publication: ReviewPublication) -> None:
        self.publications.append(publication)

    async def send_message(self, content: str) -> None:
        self.messages.append(content)

    async def publish_failure(self, public_message: str) -> None:
        self.failures.append(public_message)


@pytest.fixture
def ai_eval_settings() -> AppSettings:
    """Load the real model selection and API key without requiring a Discord connection."""

    try:
        settings = AppSettings(  # type: ignore[call-arg]
            DISCORD_TOKEN="discord-not-used-by-ai-evals",
            APP_ENV="ai-eval",
            OPENAI_MAX_RETRIES=0,
            OPENAI_TRACING_ENABLED=False,
            OPENAI_TRACE_INCLUDE_SENSITIVE_DATA=False,
        )
    except ValidationError:
        pytest.skip("OPENAI_API_KEY must be available in the environment or local .env")

    api_key = settings.openai_api_key.get_secret_value().strip()
    if len(api_key) < 20 or api_key in {"openai-test-key", "replace-me"}:
        pytest.skip("a real OPENAI_API_KEY is required for live AI evaluations")
    set_default_openai_key(api_key, use_for_tracing=False)
    return settings


async def _run_agent(
    settings: AppSettings,
    case: ReviewCase,
) -> tuple[ReviewRunContext, InMemoryPublisher]:
    model = ModelCatalog.load_default().resolve(
        settings.openai_model,
        settings.openai_reasoning_effort,
    )
    publisher = InMemoryPublisher()
    context = ReviewRunContext(case=case, publisher=publisher, attempt=1)
    persona = (await PackagedPersonaProvider().load(settings.bot_persona)).profile
    service = ReviewService(
        settings,
        ChargeCatalog.load_default(),
        ReportGuide.load_default(),
        model,
        persona,
    )

    await service.review(
        context,
        render_agent_input(case, model.runtime_defaults.image_detail),
    )

    assert publisher.failures == [], f"agent execution failed: {publisher.failures}"
    return context, publisher


def _case(*turns: tuple[str, str]) -> ReviewCase:
    """Build a realistic thread transcript from user/assistant text turns."""

    assert turns
    messages: list[ConversationMessage] = []
    for index, (role, content) in enumerate(turns):
        is_assistant = role == "assistant"
        messages.append(
            ConversationMessage(
                id=3001 + index,
                author_id=_BOT_ID if is_assistant else _REQUESTER_ID,
                author_display="Sgt. Vigil" if is_assistant else "Ayudante Torres",
                author_is_bot=is_assistant,
                author_is_assistant=is_assistant,
                content=content,
                created_at=_BASE_TIME + timedelta(minutes=index),
                is_trigger=index == len(turns) - 1,
            )
        )

    return ReviewCase(
        guild_id=4001,
        parent_channel_id=4002,
        channel_id=4003,
        thread_id=4003 if len(turns) > 1 else None,
        root_message_id=messages[0].id,
        trigger_message_id=messages[-1].id,
        requester_user_id=_REQUESTER_ID,
        requester_display_name="Elena Torres",
        requester_role_names=["Deputy Sheriff"],
        interaction_mode=(
            InteractionMode.THREAD_FOLLOW_UP
            if len(turns) > 1
            else InteractionMode.INITIAL_REVIEW
        ),
        messages=messages,
        attachments=[],
    )


def _single_publication(publisher: InMemoryPublisher) -> ReviewPublication:
    assert len(publisher.publications) == 1
    return publisher.publications[0]


async def test_approves_complete_report_without_demanding_roleplay_recordings(
    ai_eval_settings: AppSettings,
) -> None:
    case = _case(
        (
            "user",
            """Solicito la revisión de este expediente transcrito. Las fotografías reglamentarias
frontal y lateral están presentes y son legibles.

Yo, ayudante Elena Torres, bajo la asignación 241W y acompañada por el ayudante Marco Ruiz,
patrullaba Jefferson el 05/08/2026, aproximadamente a las 22:15. Observamos un Sultan negro que
circulaba sin luces y le ordenamos detenerse mediante luces y sirena. El conductor se detuvo en
Forum Drive. Cuando le indiqué que quedaba detenido por conducir de forma temeraria, volvió a
encender el vehículo y huyó hacia el este.

Iniciamos una persecución que duró ocho minutos. El vehículo quedó inmovilizado tras colisionar
contra una barrera en Idlewood. Identificamos al conductor como Daniel Vega, lo arrestamos sin
lesiones y lo trasladamos a la estación de Ganton. No se recolectaron objetos ni evidencia física.

**Se adjuntan grabaciones de las BWC de los ayudantes involucrados.**

Cargos declarados:
- CV-2801 — Evadir a un oficial de la paz.

Revisa la narrativa y los cargos.""",
        )
    )

    _, publisher = await _run_agent(ai_eval_settings, case)
    publication = _single_publication(publisher)

    assert publication.verdict is ReviewVerdict.APPROVED
    assert not any(finding.severity.value == "required" for finding in publication.findings)
    assert not any(
        finding.severity.value == "required"
        and "grabaci" in f"{finding.title} {finding.detail}".lower()
        for finding in publication.findings
    )
    assert any(charge.alignment == "supported" for charge in publication.charges)


async def test_blocks_verifiable_spelling_and_writing_errors(
    ai_eval_settings: AppSettings,
) -> None:
    case = _case(
        (
            "user",
            """Solicito revisión. Las fotografías reglamentarias están presentes.

Yo, ayudante Elena Torres, iva patrullando por Jefferson con el ayudante Marco Ruiz el 05/08/2026
a las 22:15. Hay divise un Sultan negro y le ordene detenerse. El conductor se detubo, pero cuando
le informe que quedava detenido, uyo hacia Idlewood. Lo perseguimos ocho minutos asta que choco
contra una barrera. Luego lo arrestamos y fue trasladado a la estación de Ganton. No se recolectó
evidencia física.

Cargos declarados:
- CV-2801 — Evadir a un oficial de la paz.

Revisa el expediente completo.""",
        )
    )

    _, publisher = await _run_agent(ai_eval_settings, case)
    publication = _single_publication(publisher)
    linguistic_findings = [
        finding
        for finding in publication.findings
        if any(
            term in f"{finding.title} {finding.detail} {finding.recommendation}".lower()
            for term in ("ortograf", "gramát", "redacción", "puntuación")
        )
    ]

    assert publication.verdict is ReviewVerdict.CORRECTIONS_REQUIRED
    assert linguistic_findings
    assert all(finding.severity.value == "required" for finding in linguistic_findings)


async def test_rejects_charge_when_report_explicitly_negates_its_elements(
    ai_eval_settings: AppSettings,
) -> None:
    case = _case(
        (
            "user",
            """Solicito la revisión de este expediente transcrito. Las fotografías reglamentarias
están presentes y son legibles.

Yo, ayudante Elena Torres, bajo la asignación 241W, detuve un Premier azul en Jefferson el
05/08/2026 a las 20:10 por una luz trasera averiada. El conductor se estacionó inmediatamente,
apagó el motor, entregó su documentación y permaneció junto al vehículo durante toda la
intervención. En ningún momento intentó huir, evadir la parada ni resistirse. Tras verificar una
orden judicial vigente, arresté a Andrés Soto y lo trasladé a la estación de Ganton. No se
recolectó evidencia física.

Cargos declarados:
- CV-2801 — Evadir a un oficial de la paz.

Revisa la coherencia del cargo con los hechos narrados.""",
        )
    )

    _, publisher = await _run_agent(ai_eval_settings, case)
    publication = _single_publication(publisher)

    assert publication.verdict is ReviewVerdict.CORRECTIONS_REQUIRED
    assert any(charge.alignment == "unsupported" for charge in publication.charges)


async def test_answers_manual_question_without_publishing_a_review(
    ai_eval_settings: AppSettings,
) -> None:
    case = _case(
        (
            "user",
            "¿Qué información debe contener la narrativa de un reporte de arresto según la guía?",
        )
    )

    context, publisher = await _run_agent(ai_eval_settings, case)
    response = "\n".join(publisher.messages).lower()

    assert context.interaction_completed
    assert publisher.publications == []
    assert publisher.messages
    assert any(term in response for term in ("cuándo", "fecha", "hora"))
    assert any(term in response for term in ("dónde", "ubicación", "lugar"))


async def test_re_review_uses_latest_corrected_version(
    ai_eval_settings: AppSettings,
) -> None:
    case = _case(
        (
            "user",
            "Solicito revisión. Detuve a Daniel Vega después de una persecución, pero no indiqué el lugar.",
        ),
        (
            "assistant",
            "Debes precisar dónde comenzó y terminó la intervención antes de aprobar el expediente.",
        ),
        (
            "user",
            """Presento la versión corregida para una segunda revisión. Las fotografías reglamentarias
están presentes y son legibles.

Yo, ayudante Elena Torres, bajo la asignación 241W y acompañada por el ayudante Marco Ruiz,
patrullaba Jefferson el 05/08/2026, aproximadamente a las 22:15. En Forum Drive ordenamos detenerse
al conductor de un Sultan negro. Después de detenerse, le indiqué que quedaba arrestado por
conducción temeraria; entonces encendió nuevamente el vehículo y huyó hacia el este. La persecución
terminó ocho minutos después en Idlewood, cuando el vehículo quedó inmovilizado contra una barrera.
Arrestamos a Daniel Vega sin lesiones y lo trasladamos a la estación de Ganton. No se recolectaron
objetos ni evidencia física.

Cargos declarados:
- CV-2801 — Evadir a un oficial de la paz.

Revisa únicamente el estado vigente del expediente.""",
        ),
    )

    _, publisher = await _run_agent(ai_eval_settings, case)
    publication = _single_publication(publisher)

    assert publication.verdict is ReviewVerdict.APPROVED
    assert not any(
        finding.severity.value == "required"
        and any(term in f"{finding.title} {finding.detail}".lower() for term in ("lugar", "ubicación"))
        for finding in publication.findings
    )
