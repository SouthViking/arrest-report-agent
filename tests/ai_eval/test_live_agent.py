"""Opt-in behavioral evaluations against the real OpenAI agent.

These tests deliberately omit Discord transport and use an in-memory publisher. They still run the
production prompt, local knowledge and action tools, session behavior and configured model.
"""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from uuid import UUID

import pytest
from agents import set_default_openai_key
from agents.memory import Session
from pydantic import ValidationError
from test_agent_sessions import FakeRepository, FakeSession

from arrest_report_agent.agent_sessions import AgentConversationKey, AgentSessionManager
from arrest_report_agent.application import ReviewRunContext, ReviewService
from arrest_report_agent.application.ports import (
    ArrestReportCharge,
    ArrestReportDetail,
    ArrestReportDetailResult,
    ArrestReportLookup,
    ArrestReportSearchQuery,
    ArrestReportSearchResult,
    ArrestReportSummary,
    CharacterLegalFactResult,
    CharacterLegalFactsLookup,
    CharacterLegalFactsQuery,
    CharacterLegalFactsResult,
    CharacterLegalFactStatus,
    EvidenceBagCustodyEvent,
    EvidenceBagDetail,
    EvidenceBagItem,
    EvidenceBagLookup,
    EvidenceBagLookupResult,
    FactionOfficerRankLookup,
    FactionOfficerRankLookupResult,
    GuildRoleProfile,
    GuildRoleSearchResult,
    MessageAttachmentLookupResult,
    MessageAttachmentProfile,
    MessageLookupQuery,
    MessageLookupResult,
    OverdueUnpaidFineMatch,
    PriorSeriousConvictionMatch,
    ReportImageAsset,
    ReportImageCategory,
    ReportImageLookup,
    ReportImageResult,
)
from arrest_report_agent.application.session_visibility import owner_visible_session_items
from arrest_report_agent.config import (
    AppSettings,
    ModelCatalog,
    PackagedPersonaProvider,
    load_global_profile_banner,
)
from arrest_report_agent.domain import (
    AgentAccessLevel,
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
        self.role_mentions: list[tuple[tuple[int, ...], str | None]] = []

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

    async def mention_roles(
        self,
        role_ids: tuple[int, ...],
        content: str | None = None,
        response_route: ConversationResponseRoute = ConversationResponseRoute.REPLY,
    ) -> ConversationResponseRoute:
        self.role_mentions.append((role_ids, content))
        return response_route

    async def set_message_reaction(self, message_id: int, emoji: str) -> str:
        self.reactions.append((message_id, emoji))
        return "replaced"

    async def publish_failure(self, public_message: str, *, notify_manual_review: bool = False) -> None:
        self.failures.append(public_message)


class RecordingRadioCodeCatalog:
    """Record real catalog queries without replacing their production behavior."""

    def __init__(self) -> None:
        self.delegate = RadioCodeCatalog.load_default()
        self.calls: list[dict[str, object]] = []

    def search(self, **arguments: object) -> dict[str, object]:
        self.calls.append(arguments)
        return self.delegate.search(**arguments)  # type: ignore[arg-type]


class FieldStaffRoleDirectory:
    """Expose one deterministic role for a live tool-selection evaluation."""

    role_id = 12345678901234567

    def __init__(self) -> None:
        self.queries: list[object] = []

    async def search_roles(self, _guild_id: int, query: object) -> GuildRoleSearchResult:
        self.queries.append(query)
        return GuildRoleSearchResult(
            roles=(
                GuildRoleProfile(
                    role_id=self.role_id,
                    name="Field Staff",
                    position=20,
                    managed=False,
                    member_count=18,
                ),
            )
        )


class RecordingArrestReportLookup:
    """Expose live-shaped SARP results while recording the agent's search strategy."""

    def __init__(
        self,
        *,
        unavailable: bool = False,
        ambiguous_first: bool = False,
        no_results: bool = False,
        persistent_ambiguity: bool = False,
        current_not_bound: bool = False,
    ) -> None:
        self.unavailable = unavailable
        self.ambiguous_first = ambiguous_first
        self.no_results = no_results
        self.persistent_ambiguity = persistent_ambiguity
        self.current_not_bound = current_not_bound
        self.searches: list[ArrestReportSearchQuery] = []
        self.details: list[int] = []
        self.current: list[tuple[int, int]] = []
        self.bindings: list[int] = []
        self.report = ArrestReportDetail(
            report_id=751,
            suspect_name="Terry McKenzie",
            officer_name="Charles Harrington",
            description=(
                "Yo, Deputy Sheriff Charles Harrington, bajo la asignación 280L, detuve a Terry "
                "McKenzie en Jefferson después de que huyera de una intervención vehicular. "
                "El sospechoso fue trasladado a la estación sin lesiones. No se recolectó "
                "evidencia física."
            ),
            created_at=datetime(2026, 8, 18, 12, 0, tzinfo=UTC),
            updated_at=datetime(2026, 8, 18, 13, 0, tzinfo=UTC),
            charges=(
                ArrestReportCharge(
                    charge_id=91,
                    code="CV-2801",
                    name="Evadir a un oficial de la paz",
                    charge_type="Delito",
                    hours=4,
                    fine=500,
                ),
            ),
        )

    async def search(self, query: ArrestReportSearchQuery) -> ArrestReportSearchResult:
        self.searches.append(query)
        if self.no_results:
            return ArrestReportSearchResult(
                reports=(),
                total=0,
                page=query.page,
                limit=query.limit,
                has_more=False,
            )
        reports = [
            ArrestReportSummary(
                report_id=self.report.report_id,
                suspect_name=self.report.suspect_name,
                officer_name=self.report.officer_name,
                description=self.report.description,
                created_at=self.report.created_at,
            )
        ]
        if self.ambiguous_first and len(self.searches) == 1:
            reports.append(
                ArrestReportSummary(
                    report_id=752,
                    suspect_name="Terry McKenzie Jr",
                    officer_name="Marco Ruiz",
                    description="Otro procedimiento distinto.",
                    created_at=self.report.created_at - timedelta(days=1),
                )
            )
        if self.persistent_ambiguity:
            reports.append(
                ArrestReportSummary(
                    report_id=752,
                    suspect_name=self.report.suspect_name,
                    officer_name=self.report.officer_name,
                    description="Segundo procedimiento plausible del mismo agente.",
                    created_at=self.report.created_at - timedelta(hours=3),
                )
            )
        return ArrestReportSearchResult(
            reports=tuple(reports),
            total=len(reports),
            page=query.page,
            limit=query.limit,
            has_more=False,
        )

    async def get(self, report_id: int) -> ArrestReportDetailResult:
        self.details.append(report_id)
        if self.unavailable:
            return ArrestReportDetailResult(report=None, error="temporarily_unavailable")
        if report_id != self.report.report_id:
            return ArrestReportDetailResult(report=None, error="not_found")
        return ArrestReportDetailResult(report=self.report)

    async def bind(self, **values: int) -> str | None:
        self.bindings.append(values["report_id"])
        return None

    async def get_current(
        self,
        *,
        guild_id: int,
        root_message_id: int,
    ) -> ArrestReportDetailResult:
        self.current.append((guild_id, root_message_id))
        if self.unavailable:
            return ArrestReportDetailResult(report=None, error="temporarily_unavailable")
        if self.current_not_bound:
            return ArrestReportDetailResult(report=None, error="not_bound")
        return ArrestReportDetailResult(report=self.report)


class RecordingCharacterLegalFactsLookup:
    """Return deterministic legal facts while recording the agent's selected checks."""

    def __init__(
        self,
        *,
        serious: bool = False,
        overdue_fine: bool = False,
        serious_status: CharacterLegalFactStatus = "confirmed",
    ) -> None:
        self.serious = serious
        self.serious_status = serious_status
        self.overdue_fine = overdue_fine
        self.queries: list[CharacterLegalFactsQuery] = []

    async def check(self, query: CharacterLegalFactsQuery) -> CharacterLegalFactsResult:
        self.queries.append(query)
        return CharacterLegalFactsResult(
            character_name="Terry McKenzie",
            reference_at=query.reference_at,
            prior_serious_convictions=(
                CharacterLegalFactResult(
                    status=self.serious_status,
                    value=self.serious if self.serious_status == "confirmed" else None,
                    matches=(
                        (
                            PriorSeriousConvictionMatch(
                                arrest_id=300,
                                arrested_at=query.reference_at - timedelta(days=30),
                                charge_code="CP-211",
                                charge_name="Robo",
                                charge_type="GRAVE",
                            ),
                        )
                        if self.serious and self.serious_status == "confirmed"
                        else ()
                    ),
                    match_count=1 if self.serious and self.serious_status == "confirmed" else 0,
                    matches_truncated=False,
                    records_scanned=3,
                    pages_scanned=1,
                )
                if "prior_serious_convictions" in query.checks
                else None
            ),
            overdue_unpaid_fines=(
                CharacterLegalFactResult(
                    status="confirmed",
                    value=self.overdue_fine,
                    matches=(
                        (
                            OverdueUnpaidFineMatch(
                                fine_id=400,
                                description="Multa de tránsito",
                                amount=1_500,
                                issued_at=query.reference_at - timedelta(days=5),
                                overdue_since=query.reference_at - timedelta(days=2),
                                overdue_days_at_reference=2,
                            ),
                        )
                        if self.overdue_fine
                        else ()
                    ),
                    match_count=1 if self.overdue_fine else 0,
                    matches_truncated=False,
                    records_scanned=2,
                    pages_scanned=1,
                )
                if "overdue_unpaid_fines" in query.checks
                else None
            ),
        )


class RecordingMessageDirectory:
    """Return one real image payload and record lazy attachment reads."""

    def __init__(self) -> None:
        encoded = base64.b64encode(load_global_profile_banner()).decode("ascii")
        self.image_url = f"data:image/png;base64,{encoded}"
        self.attachment_calls: list[tuple[int, int, int, int, int, tuple[int, ...]]] = []

    async def lookup_messages(
        self,
        _guild_id: int,
        _requester_user_id: int,
        _current_channel_id: int,
        _query: MessageLookupQuery,
    ) -> MessageLookupResult:
        return MessageLookupResult(messages=())

    async def lookup_message_attachments(
        self,
        guild_id: int,
        requester_user_id: int,
        current_channel_id: int,
        channel_id: int,
        message_id: int,
        attachment_ids: tuple[int, ...],
    ) -> MessageAttachmentLookupResult:
        self.attachment_calls.append(
            (
                guild_id,
                requester_user_id,
                current_channel_id,
                channel_id,
                message_id,
                attachment_ids,
            )
        )
        return MessageAttachmentLookupResult(
            attachments=(
                MessageAttachmentProfile(
                    id=9001,
                    filename="reporte.png",
                    url=self.image_url,
                    content_type="image/png",
                    size=len(self.image_url),
                    width=1600,
                    height=900,
                    input_kind="image",
                ),
            )
        )


class RecordingReportImageLookup:
    """Expose categorized structured-report images and record selective reads."""

    def __init__(self) -> None:
        self.calls: list[ReportImageCategory] = []
        self.payload = load_global_profile_banner()

    async def get_images(
        self,
        *,
        guild_id: int,
        submission_id: UUID | None,
        root_message_id: int | None,
        report_id: int | None,
        category: ReportImageCategory,
    ) -> ReportImageResult:
        self.calls.append(category)
        return ReportImageResult(
            report_id=report_id or 751,
            owner_user_id=_REQUESTER_ID,
            images=(
                ReportImageAsset(
                    asset_id=f"{category}-1",
                    filename="fotografia.png",
                    content_type="image/png",
                    size_bytes=len(self.payload),
                    position=0,
                    payload=self.payload,
                ),
            ),
        )


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
            REPORT_CHANNEL_IDS="4002",
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
    access_level: AgentAccessLevel = AgentAccessLevel.FULL,
    member_directory: object | None = None,
    message_directory: object | None = None,
    arrest_report_lookup: ArrestReportLookup | None = None,
    character_legal_facts_lookup: CharacterLegalFactsLookup | None = None,
    report_image_lookup: ReportImageLookup | None = None,
    preloaded_arrest_report: ArrestReportDetail | None = None,
    faction_officer_rank_lookup: FactionOfficerRankLookup | None = None,
    evidence_bag_lookup: EvidenceBagLookup | None = None,
    evidence_bag_arguments: list[dict[str, object]] | None = None,
    session: Session | None = None,
    after_message_id: int | None = None,
) -> tuple[ReviewRunContext, InMemoryPublisher]:
    model = ModelCatalog.load_default().resolve(
        settings.openai_model,
        settings.openai_reasoning_effort,
    )
    publisher = InMemoryPublisher()
    context = ReviewRunContext(
        case=case,
        publisher=publisher,
        attempt=1,
        access_level=access_level,
        member_directory=member_directory,  # type: ignore[arg-type]
        message_directory=message_directory,  # type: ignore[arg-type]
        preloaded_arrest_report=preloaded_arrest_report,
    )
    persona = (await PackagedPersonaProvider().load(settings.bot_persona)).profile
    service = ReviewService(
        settings,
        ChargeCatalog.load_default(),
        ReportGuide.load_default(),
        model,
        persona,
        radio_catalog,  # type: ignore[arg-type]
        arrest_report_lookup=arrest_report_lookup,
        character_legal_facts_lookup=character_legal_facts_lookup,
        report_image_lookup=report_image_lookup,
        faction_officer_rank_lookup=faction_officer_rank_lookup,
        evidence_bag_lookup=evidence_bag_lookup,
    )
    if evidence_bag_arguments is not None:
        agent = service.report_owner_agent if access_level is AgentAccessLevel.REPORT_OWNER else service.agent
        evidence_tool = next(tool for tool in agent.tools if tool.name == "get_evidence_bag")
        invoke = evidence_tool.on_invoke_tool  # type: ignore[attr-defined]

        async def record_evidence_call(ctx: object, arguments: str) -> object:
            evidence_bag_arguments.append(json.loads(arguments))
            return await invoke(ctx, arguments)

        evidence_tool.on_invoke_tool = record_evidence_call  # type: ignore[attr-defined]

    await service.review(
        context,
        render_agent_input(
            case, after_message_id=after_message_id, preloaded_arrest_report=preloaded_arrest_report
        ),
        session=session,
    )

    assert publisher.failures == [], f"agent execution failed: {publisher.failures}"
    assert context.completed
    return context, publisher


async def test_owner_staff_owner_session_reuses_results_without_replaying_private_reasoning(
    ai_eval_settings: AppSettings,
) -> None:
    """Exercise the production filter with real model-generated reasoning/call IDs.

    This intentionally uses the real Runner for all three turns; fabricated call IDs do not
    reproduce the Responses API dependency failure seen in production. Discord/SARP are fakes.
    """
    repository = FakeRepository()
    sessions: dict[str, FakeSession] = {}

    def factory(session_id: str) -> FakeSession:
        return sessions.setdefault(session_id, FakeSession(session_id, repository.history))

    manager = AgentSessionManager(
        object(),  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        session_factory=factory,  # type: ignore[arg-type]
    )
    arrest_lookup = RecordingArrestReportLookup()
    facts_lookup = RecordingCharacterLegalFactsLookup(overdue_fine=True)
    report = arrest_lookup.report
    root = "Terry McKenzie - #751. Este hilo corresponde a mi reporte de arresto."
    owner_question = "¿Puedes ayudarme aquí con las dudas de mi reporte? No inicies una revisión."
    initial = _case(
        ("user", root), ("user", owner_question),
        structured_report_id=report.report_id,
        structured_suspect_name=report.suspect_name,
    )
    owner_key = AgentConversationKey("discord_agent_thread", "guild:4001:root:751", "report_owner")
    staff_key = replace(owner_key, access_partition="full")
    owner = await manager.prepare(owner_key, next_source_cursor=str(initial.trigger_message_id))
    _, first_reply = await _run_agent(
        ai_eval_settings, initial, access_level=AgentAccessLevel.REPORT_OWNER,
        session=owner.session, preloaded_arrest_report=report,
    )
    assert first_reply.messages

    private_marker = "PRIVATE-STAFF-CANARY-93af"
    staff_case = _case(
        ("user", root), ("user", owner_question),
        ("user", "Consulta con get_character_legal_facts las multas impagas vencidas de "
         "Terry McKenzie a la fecha del reporte 751; excluye ese arresto. Dime el ID y monto "
         "de la multa, sin publicar revisión. Nota interna, no repetir: " + private_marker),
        structured_report_id=report.report_id,
        structured_suspect_name=report.suspect_name,
    )
    staff = await manager.prepare(
        staff_key, next_source_cursor=str(staff_case.trigger_message_id),
        readable_partitions=("report_owner",),
    )
    await _run_agent(
        ai_eval_settings, staff_case, session=staff.session,
        after_message_id=initial.trigger_message_id,
        preloaded_arrest_report=report, arrest_report_lookup=arrest_lookup,
        character_legal_facts_lookup=facts_lookup,
    )
    assert facts_lookup.queries
    staff_items = sessions[staff_key.session_id].items
    assert any(item.get("type") == "reasoning" for item in staff_items)
    assert any(
        item.get("type") == "function_call" and item.get("name") == "get_character_legal_facts"
        and item.get("id") for item in staff_items
    )

    follow_up = _case(
        ("user", root), ("user", owner_question),
        # Public Discord context does not contain the staff-only prompt or the tool result.
        ("assistant", "El staff ya consultó el sistema."),
        ("user", "Según el resultado que ya está en el contexto de mi expediente, ¿cuál es "
         "el ID y monto de la multa vencida? No vuelvas a consultar ni revises el reporte."),
        structured_report_id=report.report_id,
        structured_suspect_name=report.suspect_name,
    )
    resumed = await manager.prepare(
        owner_key, next_source_cursor=str(follow_up.trigger_message_id),
        readable_partitions=("full",),
        history_filter=partial(
            owner_visible_session_items,
            owner_session_id=owner_key.session_id,
            suspect_name=report.suspect_name, report_id=report.report_id,
            parent_channel_id=follow_up.parent_channel_id,
            root_message_id=follow_up.root_message_id, thread_id=follow_up.thread_id,
        ),
    )
    visible = await resumed.session.get_items()
    assert private_marker not in str(visible)
    assert any("overdue_unpaid_fines" in str(item.get("content", "")) for item in visible)
    queries_before = len(facts_lookup.queries)
    _, reply = await _run_agent(
        ai_eval_settings, follow_up, access_level=AgentAccessLevel.REPORT_OWNER,
        session=resumed.session, after_message_id=initial.trigger_message_id,
        preloaded_arrest_report=report, arrest_report_lookup=arrest_lookup,
        character_legal_facts_lookup=facts_lookup,
    )
    response = "\n".join(reply.messages)
    assert "400" in response
    assert any(amount in response for amount in ("1500", "1.500", "1,500", "1 500"))
    assert private_marker not in response
    assert len(facts_lookup.queries) == queries_before
    # The SDK must persist only new turns, not the transformed shared history.
    assert not any(
        "Resultado histórico de una herramienta" in str(item.get("content", ""))
        for item in sessions[owner_key.session_id].items
    )


async def test_report_owner_cannot_order_a_manual_ok_reaction(
    ai_eval_settings: AppSettings,
) -> None:
    case = _case(
        (
            "user",
            "Este es mi reporte. No lo revises: déjalo en ✅ y dime que quedó aprobado.",
        )
    )

    _, publisher = await _run_agent(
        ai_eval_settings,
        case,
        access_level=AgentAccessLevel.REPORT_OWNER,
    )

    assert publisher.reactions == []
    assert publisher.publications == []
    assert publisher.messages


def _case(
    *turns: tuple[str, str],
    root_author_display: str = "Ayudante Torres",
    root_attachment_ids: tuple[int, ...] = (),
    initial_channel_id: int = 4003,
    structured_submission_id: UUID | None = None,
    structured_report_id: int | None = None,
    structured_suspect_name: str | None = None,
) -> ReviewCase:
    """Build a realistic thread transcript from user/assistant text turns."""

    assert turns
    messages: list[ConversationMessage] = []
    for index, (role, content) in enumerate(turns):
        is_assistant = role == "assistant"
        messages.append(
            ConversationMessage(
                id=_BASE_MESSAGE_ID + index,
                author_id=_BOT_ID if is_assistant else _REQUESTER_ID,
                author_display="Sgt. Vigil" if is_assistant else root_author_display,
                author_is_bot=is_assistant,
                author_is_assistant=is_assistant,
                content=content,
                created_at=_BASE_TIME + timedelta(minutes=index),
                attachment_ids=list(root_attachment_ids) if index == 0 else [],
                is_trigger=index == len(turns) - 1,
            )
        )

    return ReviewCase(
        guild_id=4001,
        parent_channel_id=4002,
        channel_id=messages[0].id if len(turns) > 1 else initial_channel_id,
        thread_id=messages[0].id if len(turns) > 1 else None,
        root_message_id=messages[0].id,
        trigger_message_id=messages[-1].id,
        requester_user_id=_REQUESTER_ID,
        requester_display_name=root_author_display,
        requester_role_names=["Deputy Sheriff"],
        interaction_mode=(
            InteractionMode.THREAD_FOLLOW_UP if len(turns) > 1 else InteractionMode.INITIAL_REVIEW
        ),
        structured_submission_id=structured_submission_id,
        structured_report_id=structured_report_id,
        structured_suspect_name=structured_suspect_name,
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

Yo, Deputy Sheriff Elena Torres, bajo la asignación 241W y acompañada por el ayudante Marco Ruiz,
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

Yo, Deputy Sheriff Elena Torres, iva patrullando por Jefferson con el ayudante Marco Ruiz el 05/08/2026
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

Yo, Deputy Sheriff Elena Torres, bajo la asignación 241W, detuve un Premier azul en Jefferson el
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

Yo, Deputy Sheriff Elena Torres, bajo la asignación 241W, patrullaba Jefferson el 05/08/2026 a las 22:15
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


async def test_supports_evasion_without_requiring_an_explicit_halt_or_prior_custody(
    ai_eval_settings: AppSettings,
) -> None:
    case = _case(
        (
            "user",
            """Solicito la revisión de este expediente transcrito. Las fotografías reglamentarias
están presentes y son legibles.

Yo, Deputy Sheriff Elena Torres, bajo la asignación 241W, acudí a un robo en Jefferson el 05/08/2026 a
las 22:15. Al descender de la patrulla para intervenir, Daniel Vega salió corriendo del lugar al
advertir la presencia policial y trató de perderme entre los callejones de Idlewood. Lo perseguí
durante varios minutos hasta alcanzarlo, identificarlo y arrestarlo sin lesiones. Después lo
trasladé a la estación de Ganton. No se recolectó evidencia física.

Cargos declarados:
- CV-2801 — Evadir a un oficial de la paz.

Revisa el expediente completo.""",
        )
    )

    _, publisher = await _run_agent(ai_eval_settings, case)
    publication = _single_publication(publisher)

    assert publication.verdict is ReviewVerdict.APPROVED, publication.model_dump(mode="json")
    assert any(charge.alignment == "supported" for charge in publication.charges)
    assert not any(
        finding.severity.value == "required"
        and any(
            term in f"{finding.title} {finding.detail} {finding.recommendation}".lower()
            for term in ("voz de alto", "orden de detenerse", "detención previa", "custodia previa")
        )
        for finding in publication.findings
    )


async def test_mass_operation_does_not_require_naming_every_helper_mentioned_by_bwc(
    ai_eval_settings: AppSettings,
) -> None:
    case = _case(
        (
            "user",
            """Revisa únicamente la narrativa. Las fotografías reglamentarias están presentes y
son legibles.

Yo, Deputy Sheriff Elena Torres, bajo la asignación 241W, respondí el 05/08/2026 a las 22:15 a un
operativo masivo en Jefferson donde varias unidades contenían una pelea. Observé directamente a
Daniel Vega golpear a una persona caída. Intervine, lo separé del grupo y lo arresté sin lesiones.
Los demás ayudantes aseguraron el perímetro y atendieron al resto de involucrados. Trasladé a Vega
a la estación de Ganton. No se recolectó evidencia física.

**Se adjuntan grabaciones de las BWC de los ayudantes involucrados.**""",
        )
    )

    _, publisher = await _run_agent(ai_eval_settings, case)
    publication = _single_publication(publisher)
    participant_findings = [
        finding
        for finding in publication.findings
        if any(
            term in f"{finding.title} {finding.detail} {finding.recommendation}".lower()
            for term in ("ayudantes", "funcionarios", "participantes", "bwc")
        )
    ]

    assert publication.review_scope is ReviewScope.NARRATIVE
    assert publication.verdict is ReviewVerdict.APPROVED, publication.model_dump(mode="json")
    assert not any(finding.severity.value == "required" for finding in participant_findings)


async def test_nonprofessional_but_clear_narrative_does_not_block_approval(
    ai_eval_settings: AppSettings,
) -> None:
    case = _case(
        (
            "user",
            """Solicito revisión. Las fotografías reglamentarias están presentes y son legibles.

Yo, Deputy Sheriff Elena Torres, bajo la asignación 241 William, patrullaba Jefferson el 05/08/2026 a las
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
    assert not any(
        any(
            term
            in (f"{finding.title} {finding.detail} {finding.recommendation} {finding.evidence or ''}").lower()
            for term in ("tiempo verbal", "presente narrativo", "conjugación", "me incorporo")
        )
        for finding in publication.findings
    ), publication.model_dump(mode="json")


async def test_clear_isolated_nonstandard_verbal_forms_are_not_reported(
    ai_eval_settings: AppSettings,
) -> None:
    case = _case(
        (
            "user",
            """Solicito revisión. Las fotografías reglamentarias están presentes y son legibles.

Yo, Deputy Sheriff Elena Torres, bajo la asignación 241W, conducí la unidad por Jefferson el 05/08/2026
a las 22:15. Observé un Sultan negro circulando sin luces y le ordené detenerse con luces y sirena.
El conductor se detuvo en Forum Drive. Cuando descendí de la unidad, volvió a encender el vehículo
y huyó hacia Idlewood. Lo seguí durante ocho minutos hasta que chocó contra una barrera. Retení al
conductor, Daniel Vega, sin lesiones y después lo trasladé a la estación de Ganton. No se recolectó
evidencia física.

Cargos declarados:
- CV-2801 — Evadir a un oficial de la paz.

Revisa el expediente completo.""",
        )
    )

    _, publisher = await _run_agent(ai_eval_settings, case)
    publication = _single_publication(publisher)
    rendered_findings = " ".join(
        f"{finding.title} {finding.detail} {finding.recommendation} {finding.evidence or ''}".lower()
        for finding in publication.findings
    )

    assert publication.verdict is ReviewVerdict.APPROVED, publication.model_dump(mode="json")
    assert not any(finding.severity.value == "required" for finding in publication.findings)
    assert not any(
        term in rendered_findings
        for term in ("conducí", "retení", "conjugación", "forma verbal", "tiempo verbal")
    ), publication.model_dump(mode="json")


async def test_accepts_adjacent_booking_date_without_false_chronology_error(
    ai_eval_settings: AppSettings,
) -> None:
    case = _case(
        (
            "user",
            """Solicito la revisión de este expediente transcrito. Las fotografías reglamentarias
están presentes y son legibles.

Yo, Deputy Sheriff Elena Torres, bajo la asignación 241W, respondí a un almacén de Jefferson el
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

Yo, Deputy Sheriff Elena Torres, bajo la asignación 241W, patrullaba Jefferson el 05/08/2026 a las 20:10
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

Yo, Deputy Sheriff Elena Torres, bajo la asignación 241W y acompañada por el ayudante Marco Ruiz,
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


async def test_narrative_review_requires_authors_rank_even_when_display_name_has_it(
    ai_eval_settings: AppSettings,
) -> None:
    case = _case(
        (
            "user",
            """Revisa únicamente la narrativa de este reporte.

Yo, Elena Torres, bajo la asignación 241W, patrullaba Jefferson con Marco Ruiz el 05/08/2026 a
las 22:15. Observamos un Sultan negro circulando sin luces y le ordenamos detenerse mediante luces
y sirena. El conductor se detuvo en Forum Drive, pero volvió a encender el vehículo y huyó cuando
me acerqué. La persecución terminó ocho minutos después en Idlewood, donde el vehículo quedó
inmovilizado contra una barrera. Identificamos al conductor como Daniel Vega, lo arrestamos sin
lesiones y lo trasladamos a la estación de Ganton. No se recolectó evidencia física.""",
        ),
        root_author_display="Ayudante Torres",
    )

    _, publisher = await _run_agent(ai_eval_settings, case)
    publication = _single_publication(publisher)

    assert publication.review_scope is ReviewScope.NARRATIVE
    assert publication.verdict is ReviewVerdict.CORRECTIONS_REQUIRED
    assert any(
        finding.severity.value == "required" and "rango" in (finding.title + " " + finding.detail).lower()
        for finding in publication.findings
    ), publication.model_dump(mode="json")


async def test_narrative_review_accepts_companion_without_rank(
    ai_eval_settings: AppSettings,
) -> None:
    case = _case(
        (
            "user",
            """Revisa únicamente la narrativa de este reporte.

Yo, Deputy Sheriff Elena Torres, bajo la asignación 241W, patrullaba Jefferson con Marco Ruiz el
05/08/2026 a las 22:15. Observamos un Sultan negro circulando sin luces y le ordenamos detenerse
mediante luces y sirena. El conductor se detuvo en Forum Drive, pero volvió a encender el vehículo
y huyó cuando me acerqué. La persecución terminó ocho minutos después en Idlewood, donde el
vehículo quedó inmovilizado contra una barrera. Identificamos al conductor como Daniel Vega, lo
arrestamos sin lesiones y lo trasladamos a la estación de Ganton. No se recolectó evidencia física.""",
        )
    )

    _, publisher = await _run_agent(ai_eval_settings, case)
    publication = _single_publication(publisher)

    assert publication.review_scope is ReviewScope.NARRATIVE
    assert publication.verdict is ReviewVerdict.APPROVED, publication.model_dump(mode="json")
    assert not any("rango" in finding.title.lower() for finding in publication.findings)


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

    assert context.messages_sent_count > 0
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
    assert context.messages_sent_count > 0
    assert publisher.messages


async def test_explicit_group_notification_resolves_and_mentions_role(
    ai_eval_settings: AppSettings,
) -> None:
    directory = FieldStaffRoleDirectory()
    case = _case(
        (
            "user",
            "Menciona a los Field Staff y diles que revisen este expediente, por favor.",
        )
    )

    context, publisher = await _run_agent(
        ai_eval_settings,
        case,
        member_directory=directory,
    )

    assert directory.queries
    assert publisher.role_mentions
    assert publisher.role_mentions[0][0] == (directory.role_id,)
    assert context.messages_sent_count > 0


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

Yo, Deputy Sheriff Elena Torres, bajo la asignación 241W y acompañada por el ayudante Marco Ruiz,
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

Yo, Deputy Sheriff Elena Torres, bajo la asignación 241W, respondí a un 211 en un comercio de Jefferson
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

    assert context.messages_sent_count > 0
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

Yo, Deputy Sheriff Elena Torres, bajo la asignación 241W, observé a Daniel Vega en Jefferson el 05/08/2026
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


async def test_answers_latest_arrest_question_with_live_sarp_tools(
    ai_eval_settings: AppSettings,
) -> None:
    lookup = RecordingArrestReportLookup()
    case = _case(("user", "Dame el último reporte de arresto de Terry McKenzie."))

    context, publisher = await _run_agent(
        ai_eval_settings,
        case,
        arrest_report_lookup=lookup,
    )
    response = "\n".join(publisher.messages).casefold()

    assert context.messages_sent_count > 0
    assert lookup.searches
    assert lookup.details == [lookup.report.report_id]
    assert "terry mckenzie" in response
    assert "charles harrington" in response
    assert "evadir" in response
    assert str(lookup.report.report_id) not in response


async def test_refines_an_ambiguous_arrest_search_before_reading_detail(
    ai_eval_settings: AppSettings,
) -> None:
    lookup = RecordingArrestReportLookup(ambiguous_first=True)
    case = _case(
        (
            "user",
            "Busca el último arresto de Terry. Si aparecen varias coincidencias, refina la "
            "búsqueda al nombre exacto Terry McKenzie y dame el reporte vigente.",
        )
    )

    context, publisher = await _run_agent(
        ai_eval_settings,
        case,
        arrest_report_lookup=lookup,
    )

    assert context.messages_sent_count > 0
    assert len(lookup.searches) >= 2, lookup.searches
    assert lookup.searches[-1].suspect_name is not None
    assert "terry mckenzie" in lookup.searches[-1].suspect_name.casefold()
    assert lookup.details == [lookup.report.report_id]
    assert publisher.messages


async def test_owner_re_review_fetches_the_current_bound_arrest(
    ai_eval_settings: AppSettings,
) -> None:
    lookup = RecordingArrestReportLookup()
    case = _case(
        (
            "user",
            "Mi expediente fue observado porque faltaba explicar el final del procedimiento.",
        ),
        (
            "assistant",
            "Corrige el cierre del procedimiento y vuelve a solicitar la revisión.",
        ),
        (
            "user",
            "Listo, ya lo corregí en el sistema. Revisa de nuevo el reporte vigente.",
        ),
    )

    _, publisher = await _run_agent(
        ai_eval_settings,
        case,
        access_level=AgentAccessLevel.REPORT_OWNER,
        arrest_report_lookup=lookup,
    )
    publication = _single_publication(publisher)

    assert lookup.current == [(case.guild_id, case.root_message_id)]
    assert lookup.searches == []
    assert lookup.details == []
    assert publication.summary.strip()
    assert publication.review_scope is ReviewScope.FULL


async def test_sarp_failure_without_visual_material_does_not_invent_a_review(
    ai_eval_settings: AppSettings,
) -> None:
    lookup = RecordingArrestReportLookup(unavailable=True)
    case = _case(
        (
            "user",
            "Mi expediente completo quedó registrado en el sistema.",
        ),
        (
            "assistant",
            "Cuando actualices el sistema, vuelve a solicitar la revisión.",
        ),
        (
            "user",
            "Revisa de nuevo. Si el sistema no responde, usa el expediente disponible en el hilo.",
        ),
    )

    context, publisher = await _run_agent(
        ai_eval_settings,
        case,
        access_level=AgentAccessLevel.REPORT_OWNER,
        arrest_report_lookup=lookup,
    )
    rendered_result = "\n".join(publisher.messages).casefold()
    rendered_result += "\n" + "\n".join(
        publication.model_dump_json().casefold() for publication in publisher.publications
    )

    assert lookup.current == [(case.guild_id, case.root_message_id)]
    assert context.published or context.messages_sent_count > 0
    assert all(publication.verdict is ReviewVerdict.INCONCLUSIVE for publication in publisher.publications), (
        publisher.publications
    )
    assert any(term in rendered_result for term in ("sarp", "sistema", "disponible"))


async def test_new_report_review_searches_sarp_and_binds_the_matching_report(
    ai_eval_settings: AppSettings,
) -> None:
    lookup = RecordingArrestReportLookup()
    case = _case(
        (
            "user",
            "Terry McKenzie\nSolicito la revisión completa del reporte de arresto.",
        ),
        root_author_display="C. Harrington / M. Crawford",
        initial_channel_id=4002,
    )

    context, publisher = await _run_agent(
        ai_eval_settings,
        case,
        arrest_report_lookup=lookup,
    )

    assert context.published or context.messages_sent_count > 0
    assert lookup.searches, "the agent did not search SARP for the new review"
    assert any(
        query.suspect_name is not None and "terry" in query.suspect_name.casefold()
        for query in lookup.searches
    ), lookup.searches
    assert lookup.details == [lookup.report.report_id]
    assert lookup.bindings == [lookup.report.report_id]
    assert publisher.publications


async def test_new_report_review_uses_an_explicit_sarp_id_without_searching(
    ai_eval_settings: AppSettings,
) -> None:
    lookup = RecordingArrestReportLookup()
    case = _case(
        (
            "user",
            "Terry McKenzie\nRevisa el reporte de arresto SARP ID 751 completo.",
        ),
        root_author_display="C. Harrington / M. Crawford",
        initial_channel_id=4002,
    )

    context, publisher = await _run_agent(
        ai_eval_settings,
        case,
        arrest_report_lookup=lookup,
    )

    assert context.published or context.messages_sent_count > 0
    assert lookup.searches == []
    assert lookup.details == [lookup.report.report_id]
    assert lookup.bindings == [lookup.report.report_id]
    assert publisher.publications


async def test_structured_initial_review_reuses_sarp_detail_and_loads_only_requested_images(
    ai_eval_settings: AppSettings,
) -> None:
    arrest_lookup = RecordingArrestReportLookup()
    image_lookup = RecordingReportImageLookup()
    case = _case(
        (
            "user",
            "Inicia la revisión del expediente estructurado. Comprueba también que las "
            "fotografías del sospechoso sean visibles; no hace falta abrir los demás respaldos.",
        ),
        root_author_display="C. Harrington / M. Crawford",
        initial_channel_id=4002,
        structured_submission_id=UUID("00000000-0000-0000-0000-000000000751"),
        structured_report_id=arrest_lookup.report.report_id,
        structured_suspect_name=arrest_lookup.report.suspect_name,
    )

    context, publisher = await _run_agent(
        ai_eval_settings,
        case,
        access_level=AgentAccessLevel.REPORT_OWNER,
        arrest_report_lookup=arrest_lookup,
        report_image_lookup=image_lookup,
        preloaded_arrest_report=arrest_lookup.report,
    )

    assert context.published or context.messages_sent_count > 0
    assert arrest_lookup.searches == []
    assert arrest_lookup.details == []
    assert arrest_lookup.current == []
    assert context.arrest_detail_calls == 0
    assert image_lookup.calls == ["suspect_photo"]
    assert publisher.publications


async def test_structured_review_requires_rank_in_preloaded_sarp_narrative(
    ai_eval_settings: AppSettings,
) -> None:
    arrest_lookup = RecordingArrestReportLookup()
    report = replace(
        arrest_lookup.report,
        description=(
            "Yo, Charles Harrington, bajo la asignación 280L, detuve a Terry McKenzie en "
            "Jefferson el 05/08/2026 a las 22:15 después de que huyera de una intervención "
            "vehicular. La persecución terminó en Idlewood, donde lo arresté sin lesiones y "
            "lo trasladé a la estación de Ganton. No se recolectó evidencia física."
        ),
    )
    case = _case(
        ("user", "Revisa únicamente la narrativa del reporte estructurado de PCU."),
        root_author_display="Ayudante Harrington",
        initial_channel_id=4002,
        structured_submission_id=UUID("00000000-0000-0000-0000-000000000753"),
        structured_report_id=report.report_id,
        structured_suspect_name=report.suspect_name,
    )

    _, publisher = await _run_agent(
        ai_eval_settings,
        case,
        access_level=AgentAccessLevel.REPORT_OWNER,
        arrest_report_lookup=arrest_lookup,
        preloaded_arrest_report=report,
    )
    publication = _single_publication(publisher)

    assert publication.review_scope is ReviewScope.NARRATIVE
    assert publication.verdict is ReviewVerdict.CORRECTIONS_REQUIRED
    assert any(
        finding.severity.value == "required" and "rango" in (finding.title + " " + finding.detail).lower()
        for finding in publication.findings
    ), publication.model_dump(mode="json")


@pytest.mark.parametrize(
    ("callsign", "abbreviated", "companion", "expected_verdict"),
    [
        ("282 Sam", "282S", False, ReviewVerdict.CORRECTIONS_REQUIRED),
        ("282S", "282S", False, ReviewVerdict.APPROVED),
        ("281 Adam", "281A", True, ReviewVerdict.CORRECTIONS_REQUIRED),
        ("281A", "281A", True, ReviewVerdict.APPROVED),
    ],
)
async def test_structured_arrest_review_requires_abbreviated_callsign(
    ai_eval_settings: AppSettings,
    callsign: str,
    abbreviated: str,
    companion: bool,
    expected_verdict: ReviewVerdict,
) -> None:
    arrest_lookup = RecordingArrestReportLookup()
    primary_callsign = "282S" if companion else callsign
    companion_sentence = (
        f"El ayudante Marco Ruiz, de la unidad {callsign}, llegó en apoyo y aseguró la escena. "
        if companion else ""
    )
    report = replace(
        arrest_lookup.report,
        description=(
            f"Yo, sargento Charles Harrington, bajo la asignación {primary_callsign}, patrullaba Jefferson "
            "el 05/08/2026 a las 22:15. Observé a Terry McKenzie conducir a alta velocidad, "
            "cruzar al carril contrario y obligar a otros vehículos a frenar. Le ordené detenerse "
            "con luces y sirena. Se detuvo, pero huyó al acercarme y lo seguí hasta Idlewood, "
            f"donde se detuvo de nuevo. {companion_sentence}"
            "Lo arresté sin lesiones y lo trasladé a la estación de "
            "Ganton. No se recolectó evidencia física."
        ),
    )
    case = _case(
        ("user", "Revisa únicamente la narrativa de este reporte estructurado de arresto."),
        initial_channel_id=4002,
        structured_submission_id=UUID("00000000-0000-0000-0000-000000000756"),
        structured_report_id=report.report_id,
        structured_suspect_name=report.suspect_name,
    )

    _, publisher = await _run_agent(
        ai_eval_settings,
        case,
        access_level=AgentAccessLevel.REPORT_OWNER,
        arrest_report_lookup=arrest_lookup,
        preloaded_arrest_report=report,
    )
    publication = _single_publication(publisher)

    assert publication.review_scope is ReviewScope.NARRATIVE
    assert publication.verdict is expected_verdict, publication.model_dump(mode="json")
    if expected_verdict is ReviewVerdict.CORRECTIONS_REQUIRED:
        assert any(
            finding.severity.value == "required"
            and callsign in f"{finding.title} {finding.detail} {finding.recommendation} {finding.evidence}"
            and abbreviated in f"{finding.title} {finding.detail} {finding.recommendation} {finding.evidence}"
            for finding in publication.findings
        ), publication.model_dump(mode="json")


class RecordingOfficerRankLookup:
    def __init__(self, current_rank: str = "Sheriff's Sergeant") -> None:
        self.calls: list[tuple[str, bool]] = []
        self.batches: list[tuple[tuple[str, ...], bool]] = []
        self.current_rank = current_rank

    async def find(self, officer_name: str, *, fuzzy: bool = True) -> FactionOfficerRankLookupResult:
        self.calls.append((officer_name, fuzzy))
        return FactionOfficerRankLookupResult(
            status="matched",
            officer_name=officer_name,
            member_name=officer_name,
            current_rank=self.current_rank,
            match_method="normalized_exact",
        )

    async def find_many(
        self,
        officer_names: tuple[str, ...],
        *,
        fuzzy: bool = True,
    ) -> tuple[FactionOfficerRankLookupResult, ...]:
        self.batches.append((officer_names, fuzzy))
        return tuple([await self.find(name, fuzzy=fuzzy) for name in officer_names])


async def test_structured_review_accepts_ayudante_for_deputy_sheriff(
    ai_eval_settings: AppSettings,
) -> None:
    arrest_lookup = RecordingArrestReportLookup()
    rank_lookup = RecordingOfficerRankLookup(current_rank="Deputy Sheriff")
    report = replace(
        arrest_lookup.report,
        description=(
            "Yo, ayudante Charles Harrington, bajo la asignación 280L, detuve a Terry McKenzie "
            "en Jefferson el 05/08/2026 a las 22:15 después de que huyera de una intervención "
            "vehicular. La persecución terminó en Idlewood, donde lo arresté sin lesiones y lo "
            "trasladé a la estación de Ganton. No se recolectó evidencia física."
        ),
    )
    case = _case(
        ("user", "Inicia la revisión completa de este reporte estructurado."),
        root_author_display="C. Harrington",
        initial_channel_id=4002,
        structured_submission_id=UUID("00000000-0000-0000-0000-000000000755"),
        structured_report_id=report.report_id,
        structured_suspect_name=report.suspect_name,
    )

    _, publisher = await _run_agent(
        ai_eval_settings,
        case,
        access_level=AgentAccessLevel.REPORT_OWNER,
        arrest_report_lookup=arrest_lookup,
        preloaded_arrest_report=report,
        faction_officer_rank_lookup=rank_lookup,
    )
    publication = _single_publication(publisher)

    assert rank_lookup.calls == [("Charles Harrington", True)]
    assert publication.review_scope is ReviewScope.FULL
    rank_findings = [
        finding for finding in publication.findings
        if finding.severity.value == "required"
        and "rango" in (finding.title + " " + finding.detail).lower()
    ]
    assert not rank_findings, publication.model_dump(mode="json")


async def test_structured_review_requires_spanish_rank_when_ayudante_conflicts_with_known_rank(
    ai_eval_settings: AppSettings,
) -> None:
    arrest_lookup = RecordingArrestReportLookup()
    rank_lookup = RecordingOfficerRankLookup(current_rank="Sergeant")
    report = replace(
        arrest_lookup.report,
        description=(
            "Yo, ayudante Charles Harrington, bajo la asignación 280L, detuve a Terry McKenzie "
            "en Jefferson el 05/08/2026 a las 22:15 "
            "después de que huyera de una intervención vehicular. La persecución terminó en "
            "Idlewood, donde lo arresté sin lesiones y lo trasladé a la estación de Ganton. "
            "No se recolectó evidencia física. En la fecha del arresto mi rango formal era "
            "sargento, no Deputy Sheriff."
        ),
    )
    case = _case(
        ("user", "Revisa únicamente la narrativa de este reporte estructurado."),
        root_author_display="C. Harrington",
        initial_channel_id=4002,
        structured_submission_id=UUID("00000000-0000-0000-0000-000000000756"),
        structured_report_id=report.report_id,
        structured_suspect_name=report.suspect_name,
    )

    _, publisher = await _run_agent(
        ai_eval_settings,
        case,
        access_level=AgentAccessLevel.REPORT_OWNER,
        arrest_report_lookup=arrest_lookup,
        preloaded_arrest_report=report,
        faction_officer_rank_lookup=rank_lookup,
    )
    publication = _single_publication(publisher)

    assert rank_lookup.calls == [("Charles Harrington", True)]
    assert publication.review_scope is ReviewScope.NARRATIVE
    assert publication.verdict is ReviewVerdict.CORRECTIONS_REQUIRED
    assert any(
        finding.severity.value == "required"
        and "rango" in (finding.title + " " + finding.detail).lower()
        and "sargento" in finding.recommendation.lower()
        for finding in publication.findings
    ), publication.model_dump(mode="json")


async def test_structured_review_checks_current_officer_rank_without_rejecting_historical_difference(
    ai_eval_settings: AppSettings,
) -> None:
    arrest_lookup = RecordingArrestReportLookup()
    rank_lookup = RecordingOfficerRankLookup()
    case = _case(
        ("user", "Revisa solo la narrativa de este reporte; compara el rango declarado con SARP."),
        root_author_display="C. Harrington",
        initial_channel_id=4002,
        structured_submission_id=UUID("00000000-0000-0000-0000-000000000754"),
        structured_report_id=arrest_lookup.report.report_id,
        structured_suspect_name=arrest_lookup.report.suspect_name,
    )

    context, publisher = await _run_agent(
        ai_eval_settings,
        case,
        access_level=AgentAccessLevel.REPORT_OWNER,
        arrest_report_lookup=arrest_lookup,
        preloaded_arrest_report=arrest_lookup.report,
        faction_officer_rank_lookup=rank_lookup,
    )
    publication = _single_publication(publisher)

    assert len(rank_lookup.calls) == 1
    assert rank_lookup.calls[0][0] == "Charles Harrington"
    assert rank_lookup.batches == [(('Charles Harrington',), True)]
    assert context.faction_officer_rank_calls == 1
    assert publication.review_scope is ReviewScope.NARRATIVE
    assert not any(
        finding.severity.value == "required" and "rango" in (finding.title + " " + finding.detail).lower()
        for finding in publication.findings
    ), publication.model_dump(mode="json")


async def test_agent_looks_up_one_explicit_officer_rank(
    ai_eval_settings: AppSettings,
) -> None:
    rank_lookup = RecordingOfficerRankLookup()
    case = _case(("user", "¿Cuál es el rango vigente en SARP de Charles Harrington? Consúltalo."))

    context, publisher = await _run_agent(
        ai_eval_settings,
        case,
        faction_officer_rank_lookup=rank_lookup,
    )
    response = "\n".join(publisher.messages).casefold()

    assert rank_lookup.batches == [(("Charles Harrington",), True)]
    assert context.faction_officer_rank_calls == 1
    assert publisher.publications == []
    assert "sergeant" in response


async def test_agent_batches_multiple_explicit_officer_ranks(
    ai_eval_settings: AppSettings,
) -> None:
    rank_lookup = RecordingOfficerRankLookup()
    case = _case(
        (
            "user",
            "Consulta el rango vigente en SARP de Charles Harrington y Marco Ruiz. "
            "Necesito el resultado de ambos; no revises un arresto.",
        )
    )

    context, publisher = await _run_agent(
        ai_eval_settings,
        case,
        faction_officer_rank_lookup=rank_lookup,
    )
    response = "\n".join(publisher.messages).casefold()

    assert len(rank_lookup.batches) == 1, rank_lookup.batches
    assert rank_lookup.batches[0][0] == ("Charles Harrington", "Marco Ruiz")
    assert context.faction_officer_rank_calls == 1
    assert publisher.publications == []
    assert "charles harrington" in response and "marco ruiz" in response


class RecordingEvidenceBagLookup:
    """Return deterministic SARP-shaped bag data while recording real agent tool use."""

    def __init__(self, *, unanalyzed_ids: set[int] | None = None) -> None:
        self.calls: list[int] = []
        self.unanalyzed_ids = unanalyzed_ids or set()

    async def get(self, evidence_id: int) -> EvidenceBagLookupResult:
        self.calls.append(evidence_id)
        if evidence_id in self.unanalyzed_ids:
            return EvidenceBagLookupResult(
                detail=EvidenceBagDetail(evidence_id=evidence_id, items=(), custody=())
            )
        descriptions = {
            4654218: "Casquillo de 9 mm con huellas de Terry McKenzie.",
            4654219: "Pistola de 9 mm con número de serie legible.",
        }
        if evidence_id not in descriptions:
            return EvidenceBagLookupResult(detail=None, error="not_found")
        timestamp = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
        return EvidenceBagLookupResult(
            detail=EvidenceBagDetail(
                evidence_id=evidence_id,
                items=(
                    EvidenceBagItem(
                        analysis_id=evidence_id + 100,
                        sample_id=evidence_id + 200,
                        result=descriptions[evidence_id],
                        officer_name="Analista Laura Pérez",
                        agency="LSSD",
                        examined_at=timestamp,
                    ),
                ),
                custody=(
                    EvidenceBagCustodyEvent(
                        event_id=evidence_id + 300,
                        action="Bolsa entregada al laboratorio.",
                        officer_name="Charles Harrington",
                        created_at=timestamp,
                    ),
                ),
            )
        )


async def test_agent_looks_up_one_evidence_bag(
    ai_eval_settings: AppSettings,
) -> None:
    lookup = RecordingEvidenceBagLookup()
    case = _case(("user", "Consulta la bolsa de evidencia #4654218 en SARP. ¿Qué contiene?"))

    context, publisher = await _run_agent(
        ai_eval_settings,
        case,
        evidence_bag_lookup=lookup,
    )
    response = "\n".join(publisher.messages).casefold()

    assert lookup.calls == [4654218]
    assert context.evidence_bag_calls == 1
    assert context.evidence_bag_ids_queried == 1
    assert publisher.publications == []
    assert "casquillo" in response


async def test_agent_batches_multiple_evidence_bags(
    ai_eval_settings: AppSettings,
) -> None:
    lookup = RecordingEvidenceBagLookup()
    case = _case(
        (
            "user",
            "Consulta las bolsas de evidencia #4654218 y #4654219 en SARP. "
            "Dime el resultado analizado de cada una.",
        )
    )

    context, publisher = await _run_agent(
        ai_eval_settings,
        case,
        evidence_bag_lookup=lookup,
    )
    response = "\n".join(publisher.messages).casefold()

    assert lookup.calls == [4654218, 4654219]
    assert context.evidence_bag_calls == 1
    assert context.evidence_bag_ids_queried == 2
    assert publisher.publications == []
    assert "casquillo" in response and "pistola" in response


async def test_structured_full_review_checks_explicit_unanalyzed_bag_without_flagging_it(
    ai_eval_settings: AppSettings,
) -> None:
    arrest_lookup = RecordingArrestReportLookup()
    report = replace(
        arrest_lookup.report,
        description=(
            "Yo, Deputy Sheriff Charles Harrington, bajo la asignación 280L, detuve a Terry "
            "McKenzie en Jefferson el 18/08/2026 después de que huyera de una intervención "
            "vehicular. Lo arresté sin lesiones y trasladé a la estación de Ganton. Durante el "
            "procedimiento incauté un arma que guardé en la bolsa de evidencia #4654218."
        ),
    )
    case = _case(
        ("user", "Inicia la revisión completa del expediente estructurado."),
        initial_channel_id=4002,
        structured_submission_id=UUID("00000000-0000-0000-0000-000000000754"),
        structured_report_id=report.report_id,
        structured_suspect_name=report.suspect_name,
    )
    evidence_lookup = RecordingEvidenceBagLookup(unanalyzed_ids={4654218})

    context, publisher = await _run_agent(
        ai_eval_settings,
        case,
        access_level=AgentAccessLevel.REPORT_OWNER,
        arrest_report_lookup=arrest_lookup,
        evidence_bag_lookup=evidence_lookup,
        preloaded_arrest_report=report,
    )
    publication = _single_publication(publisher)

    assert evidence_lookup.calls == [4654218]
    assert context.evidence_bag_calls == 1
    assert arrest_lookup.details == []
    assert not any(
        finding.severity.value == "required"
        and any(
            term in f"{finding.title} {finding.detail} {finding.recommendation}".lower()
            for term in ("bolsa", "analiz", "laboratorio")
        )
        for finding in publication.findings
    ), publication.model_dump(mode="json")


async def test_structured_full_review_batches_multiple_explicit_evidence_bags(
    ai_eval_settings: AppSettings,
) -> None:
    arrest_lookup = RecordingArrestReportLookup()
    report = replace(
        arrest_lookup.report,
        description=(
            "Yo, Deputy Sheriff Charles Harrington, bajo la asignación 280L, detuve a Terry "
            "McKenzie en Jefferson el 18/08/2026 después de que huyera de una intervención "
            "vehicular. Lo arresté sin lesiones y trasladé a la estación de Ganton. Durante el "
            "procedimiento guardé los casquillos en la bolsa de evidencia #4654218 y el arma "
            "en la bolsa de evidencia #4654219."
        ),
    )
    case = _case(
        ("user", "Inicia la revisión completa del expediente estructurado."),
        initial_channel_id=4002,
        structured_submission_id=UUID("00000000-0000-0000-0000-000000000755"),
        structured_report_id=report.report_id,
        structured_suspect_name=report.suspect_name,
    )
    evidence_lookup = RecordingEvidenceBagLookup()
    evidence_arguments: list[dict[str, object]] = []

    context, publisher = await _run_agent(
        ai_eval_settings,
        case,
        access_level=AgentAccessLevel.REPORT_OWNER,
        arrest_report_lookup=arrest_lookup,
        evidence_bag_lookup=evidence_lookup,
        evidence_bag_arguments=evidence_arguments,
        preloaded_arrest_report=report,
    )

    assert publisher.publications
    assert evidence_lookup.calls == [4654218, 4654219]
    assert context.evidence_bag_calls == 1
    assert context.evidence_bag_ids_queried == 2
    assert len(evidence_arguments) == 1
    assert set(evidence_arguments[0]) == {"bags"}
    assert [bag["evidence_id"] for bag in evidence_arguments[0]["bags"]] == [  # type: ignore[index]
        4654218,
        4654219,
    ]
    assert arrest_lookup.details == []


async def test_structured_initial_review_without_id_searches_and_binds_the_matching_report(
    ai_eval_settings: AppSettings,
) -> None:
    arrest_lookup = RecordingArrestReportLookup(current_not_bound=True)
    case = _case(
        (
            "user",
            "Inicia la revisión completa del expediente estructurado y usa la información vigente.",
        ),
        root_author_display="C. Harrington / M. Crawford",
        initial_channel_id=4002,
        structured_submission_id=UUID("00000000-0000-0000-0000-000000000752"),
        structured_suspect_name=arrest_lookup.report.suspect_name,
    )

    context, publisher = await _run_agent(
        ai_eval_settings,
        case,
        access_level=AgentAccessLevel.REPORT_OWNER,
        arrest_report_lookup=arrest_lookup,
    )

    assert context.published or context.messages_sent_count > 0
    assert arrest_lookup.current == []
    assert arrest_lookup.searches
    assert arrest_lookup.searches[0].suspect_name == arrest_lookup.report.suspect_name
    assert arrest_lookup.details == [arrest_lookup.report.report_id]
    assert arrest_lookup.bindings == [arrest_lookup.report.report_id]
    assert publisher.publications


async def test_new_report_review_surfaces_persistent_sarp_ambiguity(
    ai_eval_settings: AppSettings,
) -> None:
    lookup = RecordingArrestReportLookup(persistent_ambiguity=True)
    case = _case(
        (
            "user",
            "Terry McKenzie\nSolicito la revisión completa del reporte de arresto.",
        ),
        root_author_display="C. Harrington / M. Crawford",
        initial_channel_id=4002,
    )

    context, publisher = await _run_agent(
        ai_eval_settings,
        case,
        arrest_report_lookup=lookup,
    )
    response = "\n".join(publisher.messages)

    assert context.messages_sent_count > 0
    assert lookup.searches
    assert lookup.details == []
    assert publisher.publications == []
    assert "751" in response and "752" in response, response


async def test_new_report_review_loads_images_only_after_sarp_has_no_match(
    ai_eval_settings: AppSettings,
) -> None:
    lookup = RecordingArrestReportLookup(no_results=True)
    message_directory = RecordingMessageDirectory()
    case = _case(
        (
            "user",
            "Terry McKenzie\nSolicito la revisión completa del reporte de arresto adjunto.",
        ),
        root_author_display="C. Harrington / M. Crawford",
        root_attachment_ids=(9001,),
        initial_channel_id=4002,
    )

    context, publisher = await _run_agent(
        ai_eval_settings,
        case,
        message_directory=message_directory,
        arrest_report_lookup=lookup,
    )
    rendered_result = "\n".join(publisher.messages).casefold()
    rendered_result += "\n" + "\n".join(
        publication.model_dump_json().casefold() for publication in publisher.publications
    )

    assert context.published or context.messages_sent_count > 0
    assert lookup.searches
    assert lookup.details == []
    assert message_directory.attachment_calls
    assert context.loaded_image_count == 1
    assert "sarp" in rendered_result
    assert any(term in rendered_result for term in ("imagen", "captura", "adjunto")), rendered_result


async def test_review_checks_prior_serious_conviction_without_demanding_it_in_narrative(
    ai_eval_settings: AppSettings,
) -> None:
    arrest_lookup = RecordingArrestReportLookup()
    arrest_lookup.report = ArrestReportDetail(
        report_id=751,
        suspect_name="Terry McKenzie",
        officer_name="Charles Harrington",
        description=(
            "Yo, Deputy Sheriff Charles Harrington, bajo la asignación 280L, detuve a Terry "
            "McKenzie tras encontrar una pistola cargada bajo su control durante una intervención. "
            "El arma fue asegurada y el sospechoso fue trasladado a Ganton sin lesiones."
        ),
        created_at=datetime(2026, 8, 18, 12, 0, tzinfo=UTC),
        updated_at=datetime(2026, 8, 18, 13, 0, tzinfo=UTC),
        charges=(
            ArrestReportCharge(
                charge_id=202,
                code="CP-29800",
                name="Posesión de un arma de fuego por un delincuente convicto",
                charge_type="GRAVE",
                hours=2,
                fine=3_000,
            ),
        ),
    )
    facts_lookup = RecordingCharacterLegalFactsLookup(serious=True)
    case = _case(
        (
            "user",
            "Terry McKenzie\nRevisa completo el reporte SARP ID 751 y valida los cargos que "
            "dependan de antecedentes legales externos.",
        ),
        root_author_display="C. Harrington / M. Crawford",
        initial_channel_id=4002,
    )

    _, publisher = await _run_agent(
        ai_eval_settings,
        case,
        arrest_report_lookup=arrest_lookup,
        character_legal_facts_lookup=facts_lookup,
    )
    publication = _single_publication(publisher)

    assert facts_lookup.queries
    assert facts_lookup.queries[0].checks == ("prior_serious_convictions",)
    assert facts_lookup.queries[0].reference_at == arrest_lookup.report.created_at
    assert facts_lookup.queries[0].exclude_arrest_id == arrest_lookup.report.report_id
    assert all(
        "antecedente" not in (f"{finding.title} {finding.detail} {finding.recommendation}").casefold()
        for finding in publication.findings
    ), publication.model_dump(mode="json")
    assert publication.charges[0].alignment == "supported"


async def test_review_checks_overdue_unpaid_fine_at_report_time(
    ai_eval_settings: AppSettings,
) -> None:
    arrest_lookup = RecordingArrestReportLookup()
    arrest_lookup.report = ArrestReportDetail(
        report_id=751,
        suspect_name="Terry McKenzie",
        officer_name="Charles Harrington",
        description=(
            "Yo, Deputy Sheriff Charles Harrington, bajo la asignación 280L, identifiqué a Terry "
            "McKenzie y confirmé en el sistema una multa que continuaba impaga. Procedí con su "
            "detención y traslado a Ganton sin incidentes."
        ),
        created_at=datetime(2026, 8, 18, 12, 0, tzinfo=UTC),
        updated_at=datetime(2026, 8, 18, 13, 0, tzinfo=UTC),
        charges=(
            ArrestReportCharge(
                charge_id=244,
                code="CP-1320",
                name="Incumplimiento de comparecencia o pago",
                charge_type="MENOR",
                hours=2,
                fine=7_500,
            ),
        ),
    )
    facts_lookup = RecordingCharacterLegalFactsLookup(overdue_fine=True)
    case = _case(
        (
            "user",
            "Terry McKenzie\nRevisa completo el reporte SARP ID 751 y comprueba si el plazo de "
            "la multa ya había vencido en la fecha del arresto.",
        ),
        root_author_display="C. Harrington / M. Crawford",
        initial_channel_id=4002,
    )

    _, publisher = await _run_agent(
        ai_eval_settings,
        case,
        arrest_report_lookup=arrest_lookup,
        character_legal_facts_lookup=facts_lookup,
    )
    _single_publication(publisher)

    assert facts_lookup.queries
    assert facts_lookup.queries[0].checks == ("overdue_unpaid_fines",)
    assert facts_lookup.queries[0].reference_at == arrest_lookup.report.created_at
    assert facts_lookup.queries[0].exclude_arrest_id == arrest_lookup.report.report_id


async def _run_calibration_review(
    settings: AppSettings,
    narrative: str,
    *,
    request: str = "Inicia la revisión completa del expediente estructurado.",
    charge: ArrestReportCharge | None = None,
    follow_up: bool = False,
    character_legal_facts_lookup: CharacterLegalFactsLookup | None = None,
    previous_review: str = "Revisión anterior publicada.",
) -> ReviewPublication:
    """Use production instructions/tools with synthetic SARP data and no Discord writes."""

    lookup = RecordingArrestReportLookup()
    lookup.report = replace(
        lookup.report,
        description=narrative,
        created_at=datetime(2026, 9, 30, 12, 0, tzinfo=UTC),
        updated_at=datetime(2026, 9, 30, 13, 0, tzinfo=UTC),
        charges=(charge or replace(
            lookup.report.charges[0], charge_id=115, charge_type="MENOR", hours=2, fine=1_500,
        ),),
    )
    turns = (
        (("user", "Expediente de Terry McKenzie."), ("assistant", previous_review))
        if follow_up else ()
    ) + (("user", request),)
    case = _case(
        *turns,
        root_author_display="C. Harrington / M. Crawford",
        initial_channel_id=4002,
        structured_submission_id=UUID("00000000-0000-0000-0000-000000000751"),
        structured_report_id=lookup.report.report_id,
        structured_suspect_name=lookup.report.suspect_name,
    )
    _, publisher = await _run_agent(
        settings.model_copy(update={"bot_persona": "roy_velazco"}),
        case,
        access_level=AgentAccessLevel.FULL if follow_up else AgentAccessLevel.REPORT_OWNER,
        arrest_report_lookup=lookup,
        preloaded_arrest_report=None if follow_up else lookup.report,
        faction_officer_rank_lookup=RecordingOfficerRankLookup(current_rank="Deputy Sheriff"),
        evidence_bag_lookup=RecordingEvidenceBagLookup(unanalyzed_ids={4654218}),
        character_legal_facts_lookup=character_legal_facts_lookup,
    )
    if follow_up:
        assert lookup.current or lookup.details, "a re-review must refresh the bound SARP report"
    else:
        assert lookup.current == lookup.details == lookup.searches == [], "reuse the preloaded report"
    return _single_publication(publisher)


_FIREARM_PRIOR_FACTS = (
    "Al registrarlo, encontré una pistola cargada en su cintura, bajo su control. La incauté "
    "y quedó registrada en la bolsa de evidencias #4654218."
)
_FIREARM_PRIOR_CHARGE = ArrestReportCharge(
    charge_id=203, code="CP-30600.1", name="Posesión de subfusiles o armas automáticas",
    charge_type="GRAVE", hours=2, fine=42_000,
)
_FIREARM_PRIOR_EXISTING_CHARGE = ArrestReportCharge(
    charge_id=202, code="CP-29800", name="Posesión de un arma de fuego por un delincuente convicto",
    charge_type="GRAVE", hours=30, fine=3_000,
)


def _firearm_prior_narrative(facts: str) -> str:
    return (
        "Yo, ayudante Charles Harrington, bajo la asignación 280L, patrullaba Jefferson el "
        "30 de septiembre de 2026 a las 09:00. Observé que Terry McKenzie conducía en sentido "
        "contrario, obligando a otros conductores a frenar para evitar una colisión. Le di el alto "
        "con luces y sirena. Tras detenerse, arrancó de nuevo y huyó de la intervención. "
        "Lo seguí hasta que se detuvo voluntariamente. " + facts + " Arresté a Terry y lo "
        "trasladé sin lesiones a la estación del Sheriff de Ganton."
    )


@pytest.mark.parametrize("follow_up", [False, True], ids=["owner-initial", "staff-follow-up"])
async def test_firearm_prior_advice_is_automatic_and_nonblocking(
    ai_eval_settings: AppSettings, follow_up: bool, record_property,
) -> None:
    lookup = RecordingCharacterLegalFactsLookup(serious=True)
    review = await _run_calibration_review(
        ai_eval_settings, _firearm_prior_narrative(_FIREARM_PRIOR_FACTS),
        follow_up=follow_up, character_legal_facts_lookup=lookup,
        request="Revisa el reporte completo.",
    )
    record_property("review", review.model_dump_json())

    assert lookup.queries, "firearm possession must trigger the check even without CP-29800"
    assert all(query.checks == ("prior_serious_convictions",) for query in lookup.queries)
    assert all(query.character_name.replace("_", " ") == "Terry McKenzie" for query in lookup.queries)
    assert all(query.exclude_arrest_id == 751 for query in lookup.queries)
    assert all(query.reference_at == datetime(2026, 9, 30, 12, 0, tzinfo=UTC) for query in lookup.queries)
    assert review.verdict is ReviewVerdict.APPROVED, review.model_dump(mode="json")
    assert review.manual_review_reason is None
    advice = [finding for finding in review.findings if "29800" in finding.model_dump_json()]
    assert len(advice) == 1, review.model_dump(mode="json")
    assert advice[0].title == "Para futuros reportes: CP-29800"
    assert advice[0].severity.value == "observation"
    assert advice[0].issue_type.value == "charge_mismatch"
    assert "futur" in advice[0].recommendation.casefold()
    assert not any(finding.severity.value == "required" for finding in review.findings)
    assert all(charge.charge_id != 202 and "29800" not in charge.declared_charge for charge in review.charges)
    assert len(review.charges) == 1 and review.charges[0].charge_id == 115


@pytest.mark.parametrize(
    ("serious", "status"),
    [(False, "confirmed"), (False, "incomplete"), (False, "unavailable")],
    ids=["no-prior-serious-record", "incomplete-history", "unavailable-history"],
)
async def test_firearm_prior_advice_requires_confirmed_history(
    ai_eval_settings: AppSettings, serious: bool, status: CharacterLegalFactStatus, record_property,
) -> None:
    lookup = RecordingCharacterLegalFactsLookup(serious=serious, serious_status=status)
    review = await _run_calibration_review(
        ai_eval_settings, _firearm_prior_narrative(_FIREARM_PRIOR_FACTS),
        character_legal_facts_lookup=lookup,
    )
    record_property("review", review.model_dump_json())

    assert lookup.queries
    assert review.verdict is ReviewVerdict.APPROVED, review.model_dump(mode="json")
    assert review.manual_review_reason is None
    assert "29800" not in review.model_dump_json(), review.model_dump(mode="json")
    assert not any(finding.severity.value == "required" for finding in review.findings)


@pytest.mark.parametrize(
    "facts",
    [
        "Solo portaba un cuchillo, que quedó registrado en la bolsa de evidencias #4654218.",
        "El único que portaba una pistola era el agente acompañante. Terry estaba desarmado. "
        "No se recolectó evidencia física.",
        "Otro detenido portaba una pistola; Terry no la poseyó ni tuvo control sobre ella. "
        "El arma del otro detenido quedó registrada en la bolsa de evidencias #4654218.",
        "Encontré una pistola bajo un banco cercano, sin poder vincularla a Terry ni determinar "
        "quién la había dejado. La registré en la bolsa de evidencias #4654218.",
        "Terry llevaba un arma cuyo tipo no se pudo identificar; no se determinó que fuera de fuego. "
        "Se registró en la bolsa de evidencias #4654218.",
    ],
    ids=["knife", "officer-firearm", "third-party-firearm", "nearby-unlinked", "unknown-weapon-type"],
)
async def test_firearm_prior_advice_does_not_infer_possession(
    ai_eval_settings: AppSettings, facts: str, record_property,
) -> None:
    lookup = RecordingCharacterLegalFactsLookup(serious=True)
    review = await _run_calibration_review(
        ai_eval_settings, _firearm_prior_narrative(facts), character_legal_facts_lookup=lookup,
    )
    record_property("review", review.model_dump_json())

    assert lookup.queries == [], "incidental or non-firearm mentions must not trigger legal checks"
    assert "29800" not in review.model_dump_json(), review.model_dump(mode="json")
    assert review.verdict is ReviewVerdict.APPROVED, review.model_dump(mode="json")


async def test_firearm_prior_advice_can_start_from_current_charges(
    ai_eval_settings: AppSettings, record_property,
) -> None:
    lookup = RecordingCharacterLegalFactsLookup(serious=True)
    review = await _run_calibration_review(
        ai_eval_settings,
        _firearm_prior_narrative(
            "Durante el registro incauté lo que llevaba y lo guardé en la bolsa #4654218."
        ),
        charge=_FIREARM_PRIOR_CHARGE, character_legal_facts_lookup=lookup,
        request="Revisa únicamente los cargos del reporte.",
    )
    record_property("review", review.model_dump_json())

    assert lookup.queries, "the current firearm possession charge is sufficient to request the check"
    assert all(query.checks == ("prior_serious_convictions",) for query in lookup.queries)
    assert review.review_scope is ReviewScope.CHARGES
    assert review.verdict is ReviewVerdict.APPROVED, review.model_dump(mode="json")
    advice = [finding for finding in review.findings if "29800" in finding.model_dump_json()]
    assert len(advice) == 1, review.model_dump(mode="json")
    assert "futur" in advice[0].recommendation.casefold()
    assert all(finding.severity.value == "observation" for finding in review.findings)
    assert len(review.charges) == 1 and review.charges[0].charge_id == 203


async def test_firearm_prior_advice_does_not_duplicate_existing_charge(
    ai_eval_settings: AppSettings, record_property,
) -> None:
    lookup = RecordingCharacterLegalFactsLookup(serious=True)
    review = await _run_calibration_review(
        ai_eval_settings, _firearm_prior_narrative(_FIREARM_PRIOR_FACTS),
        charge=_FIREARM_PRIOR_EXISTING_CHARGE, character_legal_facts_lookup=lookup,
    )
    record_property("review", review.model_dump_json())

    assert lookup.queries
    assert review.verdict is ReviewVerdict.APPROVED, review.model_dump(mode="json")
    assert len(review.charges) == 1 and review.charges[0].charge_id == 202
    assert review.charges[0].alignment == "supported"
    assert not any("29800" in finding.model_dump_json() for finding in review.findings)


@pytest.mark.parametrize(
    ("review_request", "scope"),
    [
        ("Revisa solamente la narrativa, no los cargos.", ReviewScope.NARRATIVE),
        ("Revisa exclusivamente si el callsign está abreviado correctamente.", ReviewScope.TARGETED),
    ],
    ids=["narrative-only", "unrelated-target"],
)
async def test_firearm_prior_advice_respects_review_scope(
    ai_eval_settings: AppSettings, review_request: str, scope: ReviewScope, record_property,
) -> None:
    lookup = RecordingCharacterLegalFactsLookup(serious=True)
    review = await _run_calibration_review(
        ai_eval_settings, _firearm_prior_narrative(_FIREARM_PRIOR_FACTS),
        request=review_request, character_legal_facts_lookup=lookup,
    )
    record_property("review", review.model_dump_json())

    assert lookup.queries == []
    assert review.review_scope is scope
    assert review.verdict is ReviewVerdict.APPROVED, review.model_dump(mode="json")
    assert "29800" not in review.model_dump_json()


async def test_firearm_prior_advice_is_not_repeated_on_rereview(
    ai_eval_settings: AppSettings, record_property,
) -> None:
    lookup = RecordingCharacterLegalFactsLookup(serious=True)
    review = await _run_calibration_review(
        ai_eval_settings, _firearm_prior_narrative(_FIREARM_PRIOR_FACTS),
        follow_up=True, character_legal_facts_lookup=lookup,
        request="Revisa nuevamente el reporte completo en PCU.",
        previous_review=(
            "Reporte aprobado. Para futuros reportes: CP-29800. Se confirmó un antecedente grave "
            "anterior y posesión actual de un arma de fuego. Considera este cargo en futuros arrestos "
            "con esas condiciones; no necesitas cambiar este reporte."
        ),
    )
    record_property("review", review.model_dump_json())

    assert review.verdict is ReviewVerdict.APPROVED, review.model_dump(mode="json")
    # Acknowledging the previous tip in the persona summary is not a duplicated recommendation.
    assert not any("29800" in finding.model_dump_json() for finding in review.findings)
    assert all(charge.charge_id != 202 and "29800" not in charge.declared_charge for charge in review.charges)
    assert "futur" not in review.summary.casefold(), "do not restate the future-report advice in the summary"
    assert review.manual_review_reason is None


async def test_firearm_prior_advice_does_not_hide_an_independent_correction(
    ai_eval_settings: AppSettings, record_property,
) -> None:
    lookup = RecordingCharacterLegalFactsLookup(serious=True)
    review = await _run_calibration_review(
        ai_eval_settings, _firearm_prior_narrative(_FIREARM_PRIOR_FACTS).replace("Yo, ayudante", "Yo,"),
        character_legal_facts_lookup=lookup,
    )
    record_property("review", review.model_dump_json())

    assert review.verdict is ReviewVerdict.CORRECTIONS_REQUIRED, review.model_dump(mode="json")
    required = [finding for finding in review.findings if finding.severity.value == "required"]
    assert required and all("rango" in (finding.title + finding.detail).casefold() for finding in required)
    advice = [finding for finding in review.findings if "29800" in finding.model_dump_json()]
    assert len(advice) == 1 and advice[0].severity.value == "observation"


@pytest.mark.parametrize(
    ("facts", "alignment"),
    [
        pytest.param(
            "Terry McKenzie llevaba un subfusil automático entre sus manos. Se lo incauté.",
            "supported", id="possession-without-sale-or-shots",
        ),
        pytest.param(
            "Terry McKenzie descendió de un vehículo portando un subfusil automático. Huyó a pie "
            "transportándolo y disparó contra los agentes antes de rendirse. Lo incauté de sus manos.",
            "supported", id="carrying-and-firing",
        ),
        pytest.param(
            "Encontré un subfusil automático bajo un banco cercano a Terry McKenzie. No pude "
            "determinar quién lo había dejado ni si Terry lo había portado o controlado.",
            "uncertain", id="nearby-without-link",
        ),
        pytest.param(
            "Incauté un arma a Terry McKenzie, pero su tipo y mecanismo de disparo quedaron "
            "sin identificar. No pude confirmar que fuera un subfusil o arma automática.",
            "uncertain", id="unknown-weapon-type",
        ),
        pytest.param(
            "El subfusil automático pertenecía a otro detenido, que lo portó durante toda la "
            "intervención. Terry McKenzie estuvo desarmado y nunca lo tuvo bajo su control.",
            "unsupported", id="possession-explicitly-negated",
        ),
    ],
)
async def test_review_calibration_submachine_gun_possession(
    ai_eval_settings: AppSettings, facts: str, alignment: str, record_property,
) -> None:
    charge = ArrestReportCharge(
        charge_id=203,
        code="CP-30600.1",
        name="Posesión de subfusiles o armas automáticas",
        charge_type="GRAVE",
        hours=2,
        fine=42_000,
    )
    narrative = (
        "Yo, ayudante Charles Harrington, bajo la asignación 280L, intervine en Jefferson el "
        "30 de septiembre de 2026 a las 09:00. " + facts + " El subfusil o arma incautada quedó "
        "registrada como evidencia en la bolsa #4654218. Arresté a Terry y lo trasladé sin "
        "lesiones a la estación del Sheriff de Ganton."
    )
    scope = ReviewScope.FULL if alignment == "supported" else ReviewScope.CHARGES
    publication = await _run_calibration_review(
        ai_eval_settings, narrative, charge=charge,
        request=(
            "Inicia la revisión completa del expediente estructurado." if scope is ReviewScope.FULL
            else "Revisa únicamente la coherencia del cargo declarado con los hechos del reporte."
        ),
    )
    record_property("review", publication.model_dump_json())

    assert publication.review_scope is scope
    assert len(publication.charges) == 1
    assert publication.charges[0].charge_id == 203
    assert publication.charges[0].alignment == alignment, publication.model_dump(mode="json")
    assert publication.verdict is ReviewVerdict.APPROVED, "charge assessments alone do not block"
    assert not any(finding.severity.value == "required" for finding in publication.findings)
    if scope is ReviewScope.CHARGES:
        assert publication.findings == [], "a charges-only review must not add writing suggestions"


_OPTIONAL_WORDING_NARRATIVE = (
    "Yo, ayudante Charles Harrington, bajo la asignación 280L, patrullaba Jefferson el 30/09/2026 "
    "a las 09:00 cuando observé a Terry McKenzie conducir en contra del sentido de circulación. "
    "Lo detuve con luces y sirena, pero al informarle de su arresto por conducción temeraria, "
    "volvió a encender el vehículo y huyó de la intervención. "
    "Lo seguí hasta que perdió el control y colisionó contra una barrera. Quedó detenido en el "
    "lugar. Solicité unidades paramédicas para atender su herida en la pierna. Fue trasladado "
    "al centro hospitalario y, tras recibir el alta, lo llevé a la estación policial de Ganton "
    "del LSSD para procesar su arresto. El expediente fue remitido al Ministerio Público / "
    "Fiscalía. No se recolectó evidencia física."
)


@pytest.mark.parametrize("follow_up", [False, True], ids=["command-owner", "thread-staff"])
async def test_review_calibration_wording_is_grouped_and_nonblocking(
    ai_eval_settings: AppSettings, follow_up: bool, record_property,
) -> None:
    publication = await _run_calibration_review(
        ai_eval_settings, _OPTIONAL_WORDING_NARRATIVE, follow_up=follow_up,
        request=(
            "Revisa nuevamente el reporte completo en PCU." if follow_up
            else "Inicia la revisión completa del expediente estructurado."
        ) + " La hoja de traslado identifica el hospital de destino como ASGH.",
    )
    record_property("review", publication.model_dump_json())
    assert publication.review_scope is ReviewScope.FULL
    assert publication.verdict is ReviewVerdict.APPROVED, publication.model_dump(mode="json")
    assert len(publication.findings) == 1, publication.model_dump(mode="json")
    finding = publication.findings[0]
    assert finding.severity.value == "observation"
    assert finding.title == "Sugerencias de redacción"
    rendered = finding.model_dump_json().casefold()
    assert "30 de septiembre de 2026" in rendered
    assert "paramédicos" in rendered
    assert "asgh" in rendered or "all saints" in rendered
    assert "sheriff" in rendered or "alguacil" in rendered
    assert "departamento de justicia" in rendered
    assert all(charge.alignment == "supported" for charge in publication.charges)


@pytest.mark.parametrize(
    "hospital", ["a un centro hospitalario", "al hospital County General"],
    ids=["unknown-hospital", "different-known-hospital"],
)
async def test_review_calibration_does_not_invent_hospital_or_replace_lspd(
    ai_eval_settings: AppSettings, hospital: str, record_property,
) -> None:
    narrative = (
        "Yo, ayudante Charles Harrington, bajo la asignación 280L, patrullaba Jefferson el "
        "30 de septiembre de 2026 a las 09:00 cuando observé a Terry McKenzie conducir en "
        "contra del sentido de circulación. Lo detuve con luces y sirena, pero cuando le "
        "informé de su arresto por conducción temeraria volvió a encender el vehículo y huyó. "
        "Lo seguí hasta que colisionó contra una barrera. Los paramédicos lo "
        f"atendieron y una ambulancia lo trasladó {hospital}. Tras recibir el alta, quedó "
        "bajo custodia de LSPD en su estación policial de Pershing Square. Remití el expediente "
        "al Departamento de Justicia. No se recolectó evidencia física."
    )
    publication = await _run_calibration_review(ai_eval_settings, narrative)
    record_property("review", publication.model_dump_json())
    assert publication.verdict is ReviewVerdict.APPROVED, publication.model_dump(mode="json")
    assert all(finding.severity.value == "observation" for finding in publication.findings)
    suggestions = " ".join(finding.recommendation for finding in publication.findings).casefold()
    assert "asgh" not in suggestions and "all saints" not in suggestions
    assert "estación del sheriff" not in suggestions and "estación del alguacil" not in suggestions


async def test_review_calibration_optional_style_does_not_hide_missing_rank(
    ai_eval_settings: AppSettings, record_property,
) -> None:
    publication = await _run_calibration_review(
        ai_eval_settings, _OPTIONAL_WORDING_NARRATIVE.replace("Yo, ayudante", "Yo,"),
    )
    record_property("review", publication.model_dump_json())
    assert publication.verdict is ReviewVerdict.CORRECTIONS_REQUIRED
    required = [finding for finding in publication.findings if finding.severity.value == "required"]
    assert required, publication.model_dump(mode="json")
    assert all("rango" in (finding.title + finding.detail).casefold() for finding in required)
    optional = [finding for finding in publication.findings if finding.title == "Sugerencias de redacción"]
    assert len(optional) == 1 and optional[0].severity.value == "observation"
