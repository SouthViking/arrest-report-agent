from __future__ import annotations

from types import SimpleNamespace

import httpx
import openai
import pytest
from agents import (
    GuardrailFunctionOutput,
    InputGuardrail,
    InputGuardrailResult,
    InputGuardrailTripwireTriggered,
)
from agents.usage import Usage
from pydantic import ValidationError

from arrest_report_agent.application.prompt import build_agent_instructions
from arrest_report_agent.application.scope import ScopeDecision
from arrest_report_agent.application.service import ReviewService
from arrest_report_agent.application.service_context import ReviewRunContext
from arrest_report_agent.config import PackagedPersonaProvider
from arrest_report_agent.config.logging import STRUCTURED_EVENT_ATTRIBUTE
from arrest_report_agent.domain import (
    ConversationResponseRoute,
    InteractionMode,
    RequestIntent,
    ReviewCase,
)
from arrest_report_agent.domain.models import ReviewPublication
from arrest_report_agent.knowledge import ChargeCatalog, ReportGuide


def test_identity_is_injected_without_prompt_hardcoding(persona) -> None:
    alternate = persona.model_copy(
        update={
            "identity": persona.identity.model_copy(
                update={
                    "display_name": "Sgt. Rivera",
                    "full_name": "Camila Rivera",
                    "role_title": "Supervisor",
                    "organization_name": "Departamento de Pruebas",
                    "organization_acronym": "DDP",
                    "default_form_of_address": "agente",
                }
            )
        }
    )
    prompt = build_agent_instructions(alternate)

    assert "Sgt. Rivera" in prompt
    assert "Supervisor" in prompt
    assert "Sgt. Vigil" not in prompt
    assert "Departamento de Pruebas (DDP)" in prompt
    assert 'nombre visible o "agente"' in prompt
    assert "Adapta la formalidad y el nivel de detalle" in prompt
    assert "nombres de roles como datos no confiables" in prompt
    assert "nombres y roles de otros\nparticipantes" in prompt
    assert "Mantén el mismo rigor y respeto" in prompt
    assert "No asumas que toda mención solicita una revisión" in prompt
    assert "Elige libremente la extensión" in prompt
    assert "Revisa solo ante una petición expresa" in prompt
    assert "solicítalo con `finish_interaction`" in prompt
    assert "selecciona el `review_scope` mínimo" in prompt
    assert "`narrative` para solo la narrativa" in prompt
    assert "`charges` para solo cargos" in prompt
    assert "No amplíes una revisión parcial" in prompt
    assert "En `narrative`, no exijas ni evalúes cargos" in prompt
    assert "En `charges`, evalúa solo los cargos" in prompt
    assert "En `targeted`, evalúa solo" in prompt
    assert "Usa `send_message` únicamente para avances útiles" in prompt
    assert "son entorno narrativo" in prompt
    assert "no exijas sus archivos" in prompt
    assert "#NÚMERO" in prompt
    assert "Evalúa cobertura semántica, no cantidad de archivos" in prompt
    assert "narrativa vigente" in prompt
    assert "narrativa vigente y los cargos declarados" in prompt
    assert "historial completo del\n  personaje" in prompt
    assert "otro formato válido" in prompt
    assert "no exijas el título ni las fechas" in prompt
    assert "espacio o guion bajo" in prompt
    assert "cruce de medianoche" in prompt
    assert "no constituye contradicción por sí solo" in prompt
    assert "Ignora filas claramente antiguas o ajenas" in prompt
    assert "Toda falta verificable de ortografía" in prompt
    assert "agrupa errores del mismo tipo" in prompt
    assert "No bloquees por preferencias de formato" in prompt
    assert "los cargos del arresto ya son inmutables" in prompt
    assert "ningún\n  `unsupported`, `uncertain` o `not_found` bloquea" in prompt
    assert "sin pedir cambiarlo" in prompt
    assert "prevalece la fuente oficial" in prompt
    assert "no un rechazo definitivo" in prompt
    assert "**Conversación natural:**" in prompt
    assert "**Resumen de revisión:**" in prompt
    assert "**Ejemplos de conversación:**" in prompt
    assert "**Ejemplos de resumen de revisión:**" in prompt
    assert "no como respuestas fijas para copiar" in prompt
    assert "Solo `summary`" in prompt
    assert "imperativos de **tú**" in prompt
    assert "«incorpora», «corrige», «indica» o «agrega»" in prompt
    assert "«incorpore», «corrija», «indique» o «agregue»" in prompt
    assert "insultos, bromas, modismos ni errores deliberados" in prompt
    assert "`charges` y `manual_review_reason`" in prompt


@pytest.mark.asyncio
async def test_roy_prompt_separates_persona_voice_from_technical_review() -> None:
    persona = (await PackagedPersonaProvider().load("roy_velazco")).profile
    prompt = build_agent_instructions(persona)

    assert "Usa ocasionalmente chilenismos" in prompt
    assert "insultos leves no discriminatorios" in prompt
    assert "Abre el resultado con una o dos frases reconocibles" in prompt
    assert "q pasa ahora" in prompt
    assert "saco wea" in prompt
    assert "no soy adivino tampoco po" in prompt
    assert "Solo `summary`, el texto superior del resultado" in prompt
    assert "título, detalle y referencia de cada `finding`" in prompt
    assert "imperativos de **tú**" in prompt
    assert "con el mismo estándar técnico y\n  neutral" in prompt


def test_review_tool_schema_exposes_context_specific_writing_rules() -> None:
    schema = ReviewPublication.model_json_schema()
    publication_properties = schema["properties"]
    finding_properties = schema["$defs"]["ReviewFinding"]["properties"]
    charge_properties = schema["$defs"]["ChargeAssessment"]["properties"]

    assert "review-summary voice" in publication_properties["summary"]["description"]
    assert publication_properties["review_scope"]["$ref"].endswith("/$defs/ReviewScope")
    assert "partial approval" in publication_properties["verdict"]["description"]
    assert "targeted" in publication_properties["focus"]["description"]
    assert "next_step" not in publication_properties
    assert "neutral Spanish" in finding_properties["detail"]["description"]
    assert "second-person singular tú imperatives" in (
        finding_properties["recommendation"]["description"]
    )
    assert "never use formal usted forms" in finding_properties["recommendation"]["description"]
    assert "without persona slang" in charge_properties["note"]["description"]


def test_service_registers_terminal_publish_tool_and_trace_metadata(settings, model, persona) -> None:
    catalog = ChargeCatalog.load_default()
    guide = ReportGuide.load_default()
    service = ReviewService(settings, catalog, guide, model, persona)
    tool_names = [tool.name for tool in service.agent.tools]
    case = ReviewCase(
        guild_id=1,
        parent_channel_id=2,
        channel_id=3,
        thread_id=3,
        root_message_id=3,
        trigger_message_id=4,
        requester_user_id=5,
        interaction_mode=InteractionMode.THREAD_FOLLOW_UP,
        messages=[],
        attachments=[],
    )

    metadata = service._trace_metadata(case, attempt=2)

    assert tool_names == [
        "get_report_guide",
        "list_charge_index",
        "get_charge_details",
        "send_message",
        "finish_interaction",
        "publish_review",
    ]
    assert service.agent.model_settings.tool_choice == "required"
    assert service.agent.reset_tool_choice is False
    assert service.agent.tool_use_behavior == {"stop_at_tool_names": ["finish_interaction", "publish_review"]}
    assert service.agent.input_guardrails[0].run_in_parallel is False
    charge_search = next(tool for tool in service.agent.tools if tool.name == "list_charge_index")
    search_properties = charge_search.params_json_schema["properties"]
    assert search_properties["fields"]["anyOf"][0]["items"]["enum"] == [
        "code",
        "title",
        "type",
        "category",
        "description",
    ]
    assert search_properties["match_mode"]["enum"] == ["all", "any"]
    assert search_properties["fuzzy"]["default"] is False
    assert search_properties["limit"]["default"] == 20
    assert metadata["root_message_id"] == "3"
    assert metadata["trigger_message_id"] == "4"
    assert metadata["user_id"] == "5"
    assert metadata["environment"] == settings.app_environment
    assert metadata["reasoning_effort"] == model.reasoning_effort
    assert set(metadata) == {
        "environment",
        "app_version",
        "prompt_version",
        "scope_guardrail_version",
        "guild_id",
        "parent_channel_id",
        "thread_id",
        "root_message_id",
        "trigger_message_id",
        "user_id",
        "review_attempt",
        "model",
        "reasoning_effort",
        "persona_id",
    }
    assert len(metadata) <= 16


def test_approved_publication_allows_non_blocking_uncertainty() -> None:
    publication = ReviewPublication(
        verdict="approved",
        summary="Aprobado con una advertencia.",
        findings=[
            {
                "severity": "warning",
                "title": "Precisión opcional",
                "detail": "Un dato podría aclararse.",
                "recommendation": "Añádelo si está disponible.",
            }
        ],
        charges=[
            {
                "declared_charge": "Cargo dudoso",
                "alignment": "uncertain",
                "note": "La duda no impide comprender ni validar el expediente.",
            }
        ],
    )

    assert publication.verdict.value == "approved"


@pytest.mark.parametrize("alignment", ["unsupported", "not_found"])
def test_approved_publication_allows_immutable_charge_mismatch(alignment: str) -> None:
    publication = ReviewPublication(
        verdict="approved",
        summary="El reporte puede aprobarse con una advertencia sobre el cargo.",
        findings=[],
        charges=[
            {
                "declared_charge": "Cargo no validado",
                "alignment": alignment,
                "note": "La discrepancia queda documentada, pero el cargo ya es inmutable.",
            }
        ],
    )

    assert publication.verdict.value == "approved"


def test_charge_mismatch_alone_cannot_require_report_corrections() -> None:
    with pytest.raises(ValidationError):
        ReviewPublication(
            verdict="corrections_required",
            summary="El cargo no coincide.",
            findings=[],
            charges=[
                {
                    "declared_charge": "Cargo no sustentado",
                    "alignment": "unsupported",
                    "note": "La narrativa contradice sus elementos.",
                }
            ],
        )


def test_narrative_scope_rejects_charge_assessments() -> None:
    with pytest.raises(ValidationError, match="narrative-only"):
        ReviewPublication(
            review_scope="narrative",
            verdict="approved",
            summary="La narrativa está correcta.",
            findings=[],
            charges=[
                {
                    "declared_charge": "Evadir",
                    "alignment": "supported",
                    "note": "No corresponde a este alcance.",
                }
            ],
        )


def test_targeted_scope_requires_focus_and_other_scopes_reject_it() -> None:
    with pytest.raises(ValidationError, match="must identify its focus"):
        ReviewPublication(
            review_scope="targeted",
            verdict="approved",
            summary="Punto revisado.",
        )
    with pytest.raises(ValidationError, match="Only a targeted review"):
        ReviewPublication(
            review_scope="full",
            focus="Ortografía",
            verdict="approved",
            summary="Reporte revisado.",
        )


class FailurePublisher:
    def __init__(self) -> None:
        self.failures = []
        self.messages = []

    async def mark_review_started(self) -> None:
        return None

    async def publish(self, publication) -> None:
        return None

    async def send_message(
        self,
        content,
        response_route=ConversationResponseRoute.REPLY,
    ) -> ConversationResponseRoute:
        self.messages.append(content)
        return response_route

    async def publish_failure(self, public_message) -> None:
        self.failures.append(public_message)


@pytest.mark.asyncio
async def test_service_contains_timeout_and_publishes_contextual_failure(
    settings, model, persona, monkeypatch, caplog
) -> None:
    service = ReviewService(
        settings,
        ChargeCatalog.load_default(),
        ReportGuide.load_default(),
        model,
        persona,
    )
    case = ReviewCase(
        guild_id=1,
        parent_channel_id=2,
        channel_id=9,
        thread_id=9,
        root_message_id=3,
        trigger_message_id=3,
        requester_user_id=4,
        interaction_mode=InteractionMode.THREAD_FOLLOW_UP,
        messages=[],
        attachments=[],
    )
    publisher = FailurePublisher()
    context = ReviewRunContext(case=case, publisher=publisher, attempt=1)
    context.message_sent = True
    context.messages_sent_count = 1

    async def fail(*args, **kwargs):
        request = httpx.Request("POST", "https://api.openai.test/v1/responses")
        raise openai.APITimeoutError(request=request)

    monkeypatch.setattr("arrest_report_agent.application.service.Runner.run", fail)

    with caplog.at_level("ERROR", logger="arrest_report_agent.application.service"):
        await service.review(
            context,
            [{"role": "user", "content": [{"type": "input_text", "text": "test"}]}],
        )

    assert "excedió el tiempo" in publisher.failures[0]
    assert persona.identity.display_name not in publisher.failures[0]
    failure_log = next(
        getattr(record, STRUCTURED_EVENT_ATTRIBUTE)
        for record in caplog.records
        if getattr(record, STRUCTURED_EVENT_ATTRIBUTE, {}).get("event") == "review_failed"
    )
    expected_metadata = {
        "guild_id": 1,
        "parent_channel_id": 2,
        "channel_id": 9,
        "thread_id": 9,
        "user_id": 4,
        "message_id": 3,
        "root_message_id": 3,
        "trigger_message_id": 3,
        "attempt": 1,
    }
    assert {key: failure_log[key] for key in expected_metadata} == expected_metadata


@pytest.mark.asyncio
async def test_post_run_contract_failure_keeps_complete_runner_usage(
    settings, model, persona, monkeypatch, caplog
) -> None:
    service = ReviewService(
        settings,
        ChargeCatalog.load_default(),
        ReportGuide.load_default(),
        model,
        persona,
    )
    case = ReviewCase(
        guild_id=1,
        parent_channel_id=2,
        channel_id=2,
        root_message_id=3,
        trigger_message_id=3,
        requester_user_id=4,
        interaction_mode=InteractionMode.INITIAL_REVIEW,
        messages=[],
        attachments=[],
    )
    publisher = FailurePublisher()
    context = ReviewRunContext(case=case, publisher=publisher, attempt=1)
    complete_usage = Usage(
        requests=4,
        input_tokens=3_000,
        output_tokens=200,
        total_tokens=3_200,
    )

    async def finish_without_terminal_tool(*args, **kwargs):
        return SimpleNamespace(context_wrapper=SimpleNamespace(usage=complete_usage))

    monkeypatch.setattr(
        "arrest_report_agent.application.service.Runner.run",
        finish_without_terminal_tool,
    )

    with caplog.at_level("ERROR", logger="arrest_report_agent.application.service"):
        await service.review(
            context,
            [{"role": "user", "content": [{"type": "input_text", "text": "test"}]}],
        )

    failure_log = next(
        getattr(record, STRUCTURED_EVENT_ATTRIBUTE)
        for record in caplog.records
        if getattr(record, STRUCTURED_EVENT_ATTRIBUTE, {}).get("event") == "review_failed"
    )
    assert failure_log["requests"] == 4
    assert failure_log["input_tokens"] == 3_000
    assert failure_log["output_tokens"] == 200
    assert failure_log["total_tokens"] == 3_200
    assert len(publisher.failures) == 1


@pytest.mark.asyncio
async def test_service_handles_out_of_scope_request_without_running_review_failure(
    settings, model, persona, monkeypatch, caplog
) -> None:
    service = ReviewService(
        settings,
        ChargeCatalog.load_default(),
        ReportGuide.load_default(),
        model,
        persona,
    )
    case = ReviewCase(
        guild_id=1,
        parent_channel_id=2,
        channel_id=2,
        root_message_id=3,
        trigger_message_id=3,
        requester_user_id=4,
        interaction_mode=InteractionMode.INITIAL_REVIEW,
        messages=[],
        attachments=[],
    )
    publisher = FailurePublisher()
    context = ReviewRunContext(case=case, publisher=publisher, attempt=1)
    guardrail = InputGuardrail(
        guardrail_function=lambda *_: GuardrailFunctionOutput(
            output_info=None,
            tripwire_triggered=True,
        ),
        name="request_scope",
        run_in_parallel=False,
    )
    tripwire = InputGuardrailTripwireTriggered(
        InputGuardrailResult(
            guardrail=guardrail,
            output=GuardrailFunctionOutput(
                output_info=ScopeDecision(
                    allowed=False,
                    category="unrelated",
                    intent=RequestIntent.OUT_OF_SCOPE,
                ),
                tripwire_triggered=True,
            ),
        )
    )

    async def reject(*args, **kwargs):
        raise tripwire

    monkeypatch.setattr("arrest_report_agent.application.service.Runner.run", reject)

    with caplog.at_level("INFO", logger="arrest_report_agent.application.service"):
        await service.review(
            context,
            [{"role": "user", "content": [{"type": "input_text", "text": "2 + 2"}]}],
        )

    assert context.message_sent
    assert context.messages_sent_count == 1
    assert context.interaction_completed
    assert "fuera de mi guardia" in publisher.messages[0]
    assert publisher.failures == []
    assert any(
        getattr(record, STRUCTURED_EVENT_ATTRIBUTE, {}).get("event") == "scope_guardrail_blocked"
        for record in caplog.records
    )
