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
    ConversationResponseRoute,
    InteractionMode,
    ReviewCase,
    ReviewPublication,
    ReviewScope,
    ReviewVerdict,
)
from arrest_report_agent.infrastructure.discord.context import render_agent_input
from arrest_report_agent.knowledge import ChargeCatalog, RadioCodeCatalog, ReportGuide

pytestmark = [pytest.mark.ai_eval, pytest.mark.asyncio(loop_scope="module")]

_REQUESTER_ID = 1001
_BOT_ID = 2001
_BASE_MESSAGE_ID = 1535817965260378142
_BASE_TIME = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


class InMemoryPublisher:
    """Capture the agent's terminal behavior without connecting to Discord."""

    def __init__(self) -> None:
        self.review_started = 0
        self.publications: list[ReviewPublication] = []
        self.messages: list[str] = []
        self.failures: list[str] = []
        self.reactions: list[tuple[int, str]] = []

    async def mark_review_started(self) -> None:
        self.review_started += 1

    async def publish(self, publication: ReviewPublication) -> None:
        self.publications.append(publication)

    async def send_message(
        self,
        content: str,
        response_route: ConversationResponseRoute = ConversationResponseRoute.REPLY,
    ) -> ConversationResponseRoute:
        self.messages.append(content)
        return response_route

    async def set_message_reaction(self, message_id: int, emoji: str) -> str:
        self.reactions.append((message_id, emoji))
        return "replaced"

    async def publish_failure(self, public_message: str) -> None:
        self.failures.append(public_message)


class RecordingRadioCodeCatalog:
    """Record real catalog queries without replacing their production behavior."""

    def __init__(self) -> None:
        self.delegate = RadioCodeCatalog.load_default()
        self.calls: list[dict[str, object]] = []

    def search(self, **arguments: object) -> dict[str, object]:
        self.calls.append(arguments)
        return self.delegate.search(**arguments)  # type: ignore[arg-type]


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
    radio_catalog: RecordingRadioCodeCatalog | None = None,
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
        radio_catalog,  # type: ignore[arg-type]
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
                id=_BASE_MESSAGE_ID + index,
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
            InteractionMode.THREAD_FOLLOW_UP if len(turns) > 1 else InteractionMode.INITIAL_REVIEW
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

    assert publication.verdict is ReviewVerdict.APPROVED, publication.model_dump(mode="json")
    assert not any(finding.severity.value == "required" for finding in publication.findings)
    assert not any(
        finding.severity.value == "required" and "grabaci" in f"{finding.title} {finding.detail}".lower()
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
        if finding.severity.value == "required"
        and any(
            error
            in (f"{finding.title} {finding.detail} {finding.recommendation} {finding.evidence or ''}").lower()
            for error in ("iva", "detubo", "quedava", "uyo", "asta")
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

    assert publication.verdict is ReviewVerdict.APPROVED
    assert any(charge.alignment == "unsupported" for charge in publication.charges)


async def test_accepts_semantically_supported_charge_without_literal_stop_order(
    ai_eval_settings: AppSettings,
) -> None:
    case = _case(
        (
            "user",
            """Solicito la revisión de este expediente transcrito. Las fotografías reglamentarias
están presentes y son legibles.

Yo, ayudante Elena Torres, bajo la asignación 241W, patrullaba Jefferson el 05/08/2026 a las 22:15
cuando observé un Sultan negro circulando a gran velocidad y cruzando ambos carriles. Activé las
luces y la sirena de la patrulla; el conductor aceleró y huyó hacia Idlewood. Lo seguí durante ocho
minutos hasta que perdió el control y quedó detenido contra una barrera. Identifiqué al conductor
como Daniel Vega, lo arresté sin lesiones y lo trasladé a la estación de Ganton. No se recolectó
evidencia física.

Cargos declarados:
- CV-2801 — Evadir a un oficial de la paz.

Revisa el expediente completo.""",
        )
    )

    _, publisher = await _run_agent(ai_eval_settings, case)
    publication = _single_publication(publisher)

    assert publication.verdict is ReviewVerdict.APPROVED, publication.model_dump(mode="json")
    assert any(charge.alignment == "supported" for charge in publication.charges)
    assert not any(finding.severity.value == "required" for finding in publication.findings)


async def test_nonprofessional_but_clear_narrative_does_not_block_approval(
    ai_eval_settings: AppSettings,
) -> None:
    case = _case(
        (
            "user",
            """Solicito revisión. Las fotografías reglamentarias están presentes y son legibles.

Yo, ayudante Elena Torres, bajo la asignación 241 William, patrullaba Jefferson el 05/08/2026 a las
22:15. La unidad 240W inició una persecución contra un Sultan negro y cuando me incorporo al apoyo
veo que el conductor toma Forum Drive hacia Idlewood. Seguimos el vehículo por unos ocho minutos.
Finalmente choca contra una barrera, así que bajamos y arrestamos a Daniel Vega sin lesiones. Luego
lo trasladamos a la estación de Ganton. No se recolectó evidencia física.

Cargos declarados:
- CV-2801 — Evadir a un oficial de la paz.

Revisa el expediente completo.""",
        )
    )

    _, publisher = await _run_agent(ai_eval_settings, case)
    publication = _single_publication(publisher)

    assert publication.verdict is ReviewVerdict.APPROVED, publication.model_dump(mode="json")
    assert not any(finding.severity.value == "required" for finding in publication.findings)


async def test_accepts_adjacent_booking_date_without_false_chronology_error(
    ai_eval_settings: AppSettings,
) -> None:
    case = _case(
        (
            "user",
            """Solicito la revisión de este expediente transcrito. Las fotografías reglamentarias
están presentes y son legibles.

Yo, ayudante Elena Torres, bajo la asignación 241W, respondí a un almacén de Jefferson el
03/08/2026, aproximadamente a las 12:00. En el interior observé a Maikel Santana preparando y
separando una sustancia controlada en varios envoltorios. Tras identificarme, Santana se entregó
sin resistencia. Incauté los envoltorios, utensilios y dinero asociado; todo quedó registrado en la
bolsa de evidencia #4829100. Arresté a Maikel Santana y lo trasladé a la estación de Ganton.

Búsqueda de arrestos: Maikel_Santana
2026-08-04 01:49:18 — Posesión de una sustancia controlada distinta de la marihuana
2026-08-04 01:49:18 — Posesión de una sustancia controlada con intención de venderla
2026-08-04 01:49:18 — Fabricación de una sustancia controlada

Revisa la narrativa y los cargos.""",
        )
    )

    _, publisher = await _run_agent(ai_eval_settings, case)
    publication = _single_publication(publisher)

    assert publication.verdict is ReviewVerdict.APPROVED, publication.model_dump(mode="json")
    assert not any(
        finding.severity.value == "required"
        and any(term in f"{finding.title} {finding.detail}".lower() for term in ("cronolog", "fecha", "hora"))
        for finding in publication.findings
    )


async def test_requires_correct_charge_history_when_modal_names_another_character(
    ai_eval_settings: AppSettings,
) -> None:
    case = _case(
        (
            "user",
            """Solicito la revisión de este expediente transcrito. Las fotografías reglamentarias
están presentes y son legibles.

Yo, ayudante Elena Torres, bajo la asignación 241W, patrullaba Jefferson el 05/08/2026 a las 20:10
cuando observé un Sultan negro circulando sin luces. Le ordené detenerse mediante luces y sirena.
El conductor se detuvo en Forum Drive, pero volvió a encender el vehículo y huyó cuando me acerqué.
Inicié una persecución que duró ocho minutos y terminó en Idlewood, donde el vehículo quedó
inmovilizado contra una barrera. Identifiqué al conductor como Maikel Santana, lo arresté sin
lesiones y lo trasladé a la estación de Ganton. No se recolectó evidencia física.

Búsqueda de arrestos: Daniel_Vega
2026-08-05 21:02:00 — Evadir a un oficial de la paz

Revisa la narrativa y los cargos.""",
        )
    )

    _, publisher = await _run_agent(ai_eval_settings, case)
    publication = _single_publication(publisher)

    assert publication.verdict is ReviewVerdict.CORRECTIONS_REQUIRED
    assert any(
        finding.severity.value == "required"
        and any(
            term
            in (f"{finding.title} {finding.detail} {finding.recommendation} {finding.evidence or ''}").lower()
            for term in (
                "nombre",
                "personaje",
                "sospechoso",
                "captura",
                "modal",
                "listado",
                "daniel",
                "maikel",
            )
        )
        for finding in publication.findings
    )


async def test_ignores_old_rows_from_the_character_charge_history(
    ai_eval_settings: AppSettings,
) -> None:
    case = _case(
        (
            "user",
            """Solicito la revisión de este expediente transcrito. Las fotografías reglamentarias
están presentes y son legibles.

Yo, ayudante Elena Torres, bajo la asignación 241W y acompañada por el ayudante Marco Ruiz,
patrullaba Jefferson el 05/08/2026 a las 22:15. Observamos un Sultan negro circulando sin luces y le
ordenamos detenerse mediante luces y sirena. El conductor se detuvo en Forum Drive, pero volvió a
encender el vehículo y huyó cuando me acerqué. Iniciamos una persecución que duró ocho minutos y
terminó en Idlewood, donde el vehículo quedó inmovilizado contra una barrera. Identificamos al
conductor como Daniel Vega, lo arrestamos sin lesiones y lo trasladamos a la estación de Ganton. No
se recolectó evidencia física.

Búsqueda de arrestos: Daniel_Vega
2026-08-05 22:30:00 — Evadir a un oficial de la paz
2026-06-10 18:20:00 — Fabricación de una sustancia controlada

Revisa la narrativa y los cargos del procedimiento actual.""",
        )
    )

    _, publisher = await _run_agent(ai_eval_settings, case)
    publication = _single_publication(publisher)

    assert publication.verdict is ReviewVerdict.APPROVED, publication.model_dump(mode="json")
    assert not any("fabricación" in charge.declared_charge.lower() for charge in publication.charges)


async def test_narrative_only_review_does_not_require_or_assess_charges(
    ai_eval_settings: AppSettings,
) -> None:
    case = _case(
        (
            "user",
            """Revisa únicamente la narrativa; no evalúes los cargos.

Yo, ayudante Elena Torres, bajo la asignación 241W, patrullaba Jefferson el 05/08/2026 a las 22:15
cuando observé un Sultan negro circulando sin luces. Le ordené detenerse mediante luces y sirena.
El conductor se detuvo en Forum Drive, pero volvió a encender el vehículo y huyó cuando me acerqué.
Inicié una persecución que duró ocho minutos y terminó en Idlewood, donde el vehículo quedó
inmovilizado contra una barrera. Identifiqué al conductor como Daniel Vega, lo arresté sin lesiones
y lo trasladé a la estación de Ganton. No se recolectó evidencia física.""",
        )
    )

    _, publisher = await _run_agent(ai_eval_settings, case)
    publication = _single_publication(publisher)

    assert publication.review_scope is ReviewScope.NARRATIVE
    assert publication.verdict is ReviewVerdict.APPROVED
    assert publication.charges == []


async def test_charges_only_review_ignores_general_writing_errors(
    ai_eval_settings: AppSettings,
) -> None:
    case = _case(
        (
            "user",
            """Evalúa solamente los cargos y no revises la redacción general.

Yo iva patrullando Jefferson el 05/08/2026 a las 22:15 cuando ordené detenerse a Daniel Vega. El
conductor se detubo, pero volvió a encender el Sultan y huyó durante ocho minutos hasta Idlewood.

Búsqueda de arrestos: Daniel_Vega
2026-08-05 22:30:00 — Evadir a un oficial de la paz""",
        )
    )

    _, publisher = await _run_agent(ai_eval_settings, case)
    publication = _single_publication(publisher)

    assert publication.review_scope is ReviewScope.CHARGES
    assert publication.verdict is ReviewVerdict.APPROVED
    assert publication.charges
    assert not any(finding.severity.value == "required" for finding in publication.findings)


async def test_targeted_re_review_does_not_reopen_findings_outside_focus(
    ai_eval_settings: AppSettings,
) -> None:
    case = _case(
        (
            "user",
            "El reporte no indicó dónde terminó la persecución y además tenía errores ortográficos.",
        ),
        (
            "assistant",
            "Debes agregar la ubicación final y corregir la ortografía antes de una revisión completa.",
        ),
        (
            "user",
            """Reevalúa únicamente si corregí la ubicación; no revises nuevamente la ortografía.

Yo iva patrullando Jefferson cuando Daniel Vega huyó en un Sultan. La persecución terminó en
Idlewood, junto a la estación de servicio, donde el vehículo quedó inmovilizado.""",
        ),
    )

    _, publisher = await _run_agent(ai_eval_settings, case)
    publication = _single_publication(publisher)

    assert publication.review_scope is ReviewScope.TARGETED
    assert publication.focus is not None
    assert publication.verdict is ReviewVerdict.APPROVED, publication.model_dump(mode="json")
    assert not any(
        any(term in f"{finding.title} {finding.detail}".lower() for term in ("ortograf", "redacción", "iva"))
        for finding in publication.findings
    )


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


async def test_indirect_status_override_updates_root_reaction_without_re_review(
    ai_eval_settings: AppSettings,
) -> None:
    case = _case(
        (
            "user",
            "El reporte de Antonio Caruso quedó con una corrección menor de ortografía.",
        ),
        (
            "assistant",
            "El expediente queda con correcciones requeridas por ese detalle de redacción.",
        ),
        (
            "user",
            "Déjalo marcado como OK; vamos a omitir ese detalle menor.",
        ),
    )

    context, publisher = await _run_agent(ai_eval_settings, case)

    assert publisher.reactions == [(case.root_message_id, "✅")]
    assert publisher.publications == []
    assert context.interaction_completed
    assert publisher.messages


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


async def test_resolves_radio_codes_before_assessing_report_charges(
    ai_eval_settings: AppSettings,
) -> None:
    radio_catalog = RecordingRadioCodeCatalog()
    case = _case(
        (
            "user",
            """Solicito la revisión completa. Las fotografías frontal y lateral están presentes.

Yo, ayudante Elena Torres, bajo la asignación 241W, respondí a un 211 en un comercio de Jefferson
el 05/08/2026 a las 20:15. La encargada señaló a Daniel Vega y declaró que acababa de quitarle el
dinero de la caja mediante un forcejeo. Lo observé huir con el efectivo, lo alcancé a una cuadra y
lo arresté sin lesiones. Informé el 10-15 y regresé a disponibilidad con un 10-98. El detenido fue
trasladado a la estación de Ganton. El dinero quedó asociado a la bolsa de evidencia #1842.

Cargos declarados:
- CP-459 — Robo.

Revisa la narrativa y los cargos.""",
        )
    )

    _, publisher = await _run_agent(ai_eval_settings, case, radio_catalog)
    publication = _single_publication(publisher)
    queried = {
        str(query).casefold().replace("-", "").replace(" ", "")
        for call in radio_catalog.calls
        for query in call["queries"]  # type: ignore[union-attr]
    }

    assert {"211", "1015", "1098"}.issubset(queried), radio_catalog.calls
    assert publication.verdict is ReviewVerdict.APPROVED, publication.model_dump(mode="json")
    assert any(charge.alignment == "supported" for charge in publication.charges)


async def test_answers_batched_radio_and_phonetic_question_from_official_catalog(
    ai_eval_settings: AppSettings,
) -> None:
    radio_catalog = RecordingRadioCodeCatalog()
    case = _case(
        (
            "user",
            "Según la referencia radial oficial, ¿qué significan 10-20 y 902R, y qué letras son "
            "Adam y Ocean?",
        )
    )

    context, publisher = await _run_agent(ai_eval_settings, case, radio_catalog)
    response = "\n".join(publisher.messages).casefold()
    queried = {
        str(query).casefold().replace("-", "").replace(" ", "")
        for call in radio_catalog.calls
        for query in call["queries"]  # type: ignore[union-attr]
    }

    assert context.interaction_completed
    assert publisher.publications == []
    assert {"1020", "902r", "adam", "ocean"}.issubset(queried), radio_catalog.calls
    assert "ubicación" in response
    assert "ambulancia" in response
    assert "a" in response and "adam" in response
    assert "o" in response and "ocean" in response


async def test_does_not_treat_dates_times_evidence_or_charge_ids_as_radio_codes(
    ai_eval_settings: AppSettings,
) -> None:
    radio_catalog = RecordingRadioCodeCatalog()
    case = _case(
        (
            "user",
            """Revisa esta narrativa. Las fotografías están presentes.

Yo, ayudante Elena Torres, bajo la asignación 241W, observé a Daniel Vega en Jefferson el 05/08/2026
a las 21:15 mientras circulaba a alta velocidad, cruzaba repetidamente al carril contrario y obligaba
a otros vehículos a frenar para evitar una colisión. Le ordené detenerse mediante luces y sirena. El
conductor se detuvo de inmediato, fue arrestado sin resistencia y trasladado a Ganton. Una botella
abierta recuperada del habitáculo quedó registrada en la bolsa de evidencia #211.

Cargos declarados:
- CV-23103 — Conducción temeraria.""",
        )
    )

    _, publisher = await _run_agent(ai_eval_settings, case, radio_catalog)
    publication = _single_publication(publisher)

    assert radio_catalog.calls == []
    assert publication.verdict is ReviewVerdict.APPROVED, publication.model_dump(mode="json")
