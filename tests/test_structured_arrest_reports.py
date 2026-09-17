from __future__ import annotations

import asyncio
import io
import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import discord
import pytest
from agents import ToolOutputImage, ToolOutputText
from agents.tool_context import ToolContext
from PIL import Image

from arrest_report_agent.application.ports import (
    ArrestReportCharge,
    ArrestReportDetail,
    ArrestReportDetailResult,
    ArrestReportSearchResult,
    ArrestReportSummary,
    ReportImageAsset,
    ReportImageResult,
)
from arrest_report_agent.application.service_context import ReviewRunContext
from arrest_report_agent.application.tools import build_agent_tools
from arrest_report_agent.config import ArrestReportAttachmentMode
from arrest_report_agent.domain import (
    AgentAccessLevel,
    InteractionMode,
    ReviewCase,
    ReviewVerdict,
)
from arrest_report_agent.infrastructure.postgres.schema import arrest_report_submissions
from arrest_report_agent.infrastructure.storage import MemoryObjectStorage
from arrest_report_agent.knowledge import ChargeCatalog, ReportGuide
from arrest_report_agent.structured_arrest_reports import (
    ArrestReportAsset,
    ArrestReportAssetCategory,
    ArrestReportSubmission,
    InitialReviewStatus,
    PublicationStatus,
    StructuredArrestReportRepository,
    StructuredArrestReportService,
    StructuredReportSnapshotManager,
)
from arrest_report_agent.structured_arrest_reports.attachments import build_discord_files
from arrest_report_agent.structured_arrest_reports.discord import (
    StructuredArrestReportCog,
    StructuredArrestReportModal,
    _availability_denial,
    _replace_deferred_with_ephemeral_error,
    _thread_name,
    build_submission_view,
)
from arrest_report_agent.structured_arrest_reports.render import (
    _charge_badge,
    _description_paragraphs,
    render_arrest_report_image,
)
from arrest_report_agent.structured_arrest_reports.service import StructuredReportFile

_SUBMISSION_ID = uuid.UUID("00000000-0000-0000-0000-000000000321")


class FakeRepository:
    def __init__(self, submission: ArrestReportSubmission) -> None:
        self.submission = submission

    async def replace_assets(
        self,
        submission_id: uuid.UUID,
        pending: tuple[Any, ...],
    ) -> tuple[ArrestReportAsset, ...]:
        now = datetime.now(UTC)
        assets = tuple(
            ArrestReportAsset(
                id=uuid.uuid4(),
                submission_id=submission_id,
                created_at=now,
                **asset.model_dump(),
            )
            for asset in pending
        )
        self.submission = self.submission.model_copy(update={"assets": assets})
        return assets

    async def get_by_root(
        self,
        guild_id: int,
        root_message_id: int,
    ) -> ArrestReportSubmission | None:
        if guild_id == self.submission.guild_id and root_message_id == self.submission.root_message_id:
            return self.submission
        return None

    async def get_by_id(self, submission_id: uuid.UUID) -> ArrestReportSubmission | None:
        if submission_id == self.submission.id:
            return self.submission
        return None

    async def get_by_report(
        self,
        guild_id: int,
        report_id: int,
    ) -> ArrestReportSubmission | None:
        if guild_id == self.submission.guild_id and report_id == self.submission.sarp_arrest_id:
            return self.submission
        return None


class FakeImageLookup:
    def __init__(self, *, error: str | None = None, empty: bool = False) -> None:
        self.calls: list[tuple[uuid.UUID | None, int | None, int | None, str]] = []
        self.error = error
        self.empty = empty

    async def get_images(
        self,
        *,
        guild_id: int,
        submission_id: uuid.UUID | None,
        root_message_id: int | None,
        report_id: int | None,
        category: str,
    ) -> ReportImageResult:
        self.calls.append((submission_id, root_message_id, report_id, category))
        images = (
            ()
            if self.empty or self.error is not None
            else (
                ReportImageAsset(
                    asset_id=f"{category}-1",
                    filename="image.png",
                    content_type="image/png",
                    size_bytes=8,
                    position=0,
                    payload=b"png-data",
                ),
            )
        )
        return ReportImageResult(
            report_id=321,
            owner_user_id=6,
            images=images,
            error=self.error,
        )


class FakeArrestLookup:
    def __init__(self) -> None:
        self.current_calls = 0
        self.searches: list[Any] = []
        self.binds: list[dict[str, Any]] = []
        self.report = ArrestReportDetail(
            report_id=321,
            suspect_name="Andy Quintero",
            officer_name="Charles Harrington",
            description="Yo, Charles Harrington, procedí al arresto.",
            created_at=datetime.now(UTC),
            updated_at=None,
            charges=(),
        )

    async def search(self, query: Any) -> ArrestReportSearchResult:
        self.searches.append(query)
        return ArrestReportSearchResult(
            reports=(
                ArrestReportSummary(
                    report_id=self.report.report_id,
                    suspect_name=self.report.suspect_name,
                    officer_name=self.report.officer_name,
                    description=self.report.description,
                    created_at=self.report.created_at,
                ),
            ),
            total=1,
            page=1,
            limit=10,
            has_more=False,
        )

    async def get(self, report_id: int) -> ArrestReportDetailResult:
        if report_id != self.report.report_id:
            return ArrestReportDetailResult(report=None, error="not_found")
        return ArrestReportDetailResult(report=self.report)

    async def bind(self, **values: Any) -> None:
        self.binds.append(values)
        return None

    async def get_current(self, **_values: Any) -> ArrestReportDetailResult:
        self.current_calls += 1
        return ArrestReportDetailResult(report=None, error="not_bound")


def _submission(report_id: int | None = 321) -> ArrestReportSubmission:
    now = datetime.now(UTC)
    return ArrestReportSubmission(
        id=_SUBMISSION_ID,
        guild_id=1,
        interaction_id=2,
        owner_user_id=6,
        parent_channel_id=3,
        root_message_id=4,
        thread_id=4,
        sarp_arrest_id=report_id,
        suspect_name="Andy Quintero",
        publication_status=PublicationStatus.PUBLISHED,
        initial_review_status=InitialReviewStatus.PENDING,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_publication_transition_is_committed_in_a_transaction() -> None:
    transaction_connection = object()
    transaction_completed = False
    fetch_connections: list[object | None] = []
    now = datetime.now(UTC)

    class Client:
        @asynccontextmanager
        async def transaction(self):
            nonlocal transaction_completed
            yield transaction_connection
            transaction_completed = True

        async def fetch_one(self, _statement: object, *, connection: object | None = None):
            fetch_connections.append(connection)
            return {
                "id": _SUBMISSION_ID,
                "guild_id": 1,
                "interaction_id": 2,
                "owner_user_id": 6,
                "parent_channel_id": 3,
                "root_message_id": 4,
                "thread_id": 4,
                "sarp_arrest_id": 321,
                "suspect_name": "Andy Quintero",
                "publication_status": PublicationStatus.PUBLISHED.value,
                "initial_review_status": InitialReviewStatus.PENDING.value,
                "created_at": now,
                "updated_at": now,
            }

    repository = StructuredArrestReportRepository(Client())  # type: ignore[arg-type]

    published = await repository.mark_published(
        _SUBMISSION_ID,
        root_message_id=4,
        thread_id=4,
    )

    assert transaction_completed is True
    assert fetch_connections == [transaction_connection]
    assert published.publication_status is PublicationStatus.PUBLISHED
    assert published.root_message_id == 4
    assert published.thread_id == 4


@pytest.mark.asyncio
async def test_uncommitted_publication_mapping_is_recovered_from_its_binding() -> None:
    now = datetime.now(UTC)
    pending_row = {
        "id": _SUBMISSION_ID,
        "guild_id": 1,
        "interaction_id": 2,
        "owner_user_id": 6,
        "parent_channel_id": 3,
        "root_message_id": None,
        "thread_id": None,
        "sarp_arrest_id": 321,
        "suspect_name": "Andy Quintero",
        "publication_status": PublicationStatus.PUBLISHING.value,
        "initial_review_status": InitialReviewStatus.COMPLETED.value,
        "created_at": now,
        "updated_at": now,
    }
    binding_row = {
        "guild_id": 1,
        "root_message_id": 4,
        "parent_channel_id": 3,
        "thread_id": 4,
        "sarp_arrest_id": 321,
        "bound_by_user_id": 6,
        "trigger_message_id": 4,
        "created_at": now,
        "updated_at": now,
    }
    published_row = {
        **pending_row,
        "root_message_id": 4,
        "thread_id": 4,
        "publication_status": PublicationStatus.PUBLISHED.value,
    }

    class Result:
        def __init__(self, rows: list[dict[str, Any]]) -> None:
            self.rows = rows

        def mappings(self):
            return self

        def one_or_none(self):
            assert len(self.rows) <= 1
            return self.rows[0] if self.rows else None

        def all(self):
            return self.rows

    class Connection:
        def __init__(self) -> None:
            self.results = iter(
                (
                    Result([binding_row]),
                    Result([pending_row]),
                    Result([published_row]),
                )
            )
            self.statements: list[object] = []

        async def execute(self, statement: object):
            self.statements.append(statement)
            return next(self.results)

    connection = Connection()

    class Client:
        @asynccontextmanager
        async def transaction(self):
            yield connection

    repository = StructuredArrestReportRepository(Client())  # type: ignore[arg-type]

    recovered = await repository.recover_published_mapping(
        guild_id=1,
        root_message_id=4,
        thread_id=4,
    )

    assert recovered is not None
    assert recovered.publication_status is PublicationStatus.PUBLISHED
    assert recovered.root_message_id == 4
    assert recovered.thread_id == 4
    assert len(connection.statements) == 3


@pytest.mark.asyncio
async def test_published_structured_roots_are_resolved_in_one_query() -> None:
    now = datetime.now(UTC)
    rows = [
        {
            "id": _SUBMISSION_ID,
            "guild_id": 1,
            "interaction_id": 2,
            "owner_user_id": 6,
            "parent_channel_id": 3,
            "root_message_id": 4,
            "thread_id": 5,
            "sarp_arrest_id": 321,
            "suspect_name": "Andy Quintero",
            "publication_status": PublicationStatus.PUBLISHED.value,
            "initial_review_status": InitialReviewStatus.COMPLETED.value,
            "created_at": now,
            "updated_at": now,
        }
    ]

    class Result:
        def mappings(self):
            return self

        def all(self):
            return rows

    class Connection:
        def __init__(self) -> None:
            self.statements: list[object] = []

        async def execute(self, statement: object):
            self.statements.append(statement)
            return Result()

    connection = Connection()

    class Client:
        @asynccontextmanager
        async def connection(self):
            yield connection

    repository = StructuredArrestReportRepository(Client())  # type: ignore[arg-type]

    submissions = await repository.get_published_by_roots(1, (4, 4, 999))

    assert len(connection.statements) == 1
    assert [submission.root_message_id for submission in submissions] == [4]
    assert submissions[0].owner_user_id == 6
    assert submissions[0].assets == ()


@pytest.mark.asyncio
async def test_empty_structured_root_lookup_does_not_open_a_connection() -> None:
    class Client:
        @asynccontextmanager
        async def connection(self):
            raise AssertionError("The database must not be queried")
            yield

    repository = StructuredArrestReportRepository(Client())  # type: ignore[arg-type]

    assert await repository.get_published_by_roots(1, ()) == ()


def _context(access_level: AgentAccessLevel) -> ReviewRunContext:
    return ReviewRunContext(
        case=ReviewCase(
            guild_id=1,
            parent_channel_id=3,
            channel_id=4,
            thread_id=4,
            root_message_id=4,
            trigger_message_id=5,
            requester_user_id=6,
            interaction_mode=InteractionMode.THREAD_FOLLOW_UP,
            structured_submission_id=_SUBMISSION_ID,
            structured_report_id=321,
            structured_suspect_name="Andy Quintero",
            messages=[],
            attachments=[],
        ),
        publisher=SimpleNamespace(),
        attempt=1,
        access_level=access_level,
    )


def _bundle(*, enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(
            report_owner_access_max_rounds=5,
            report_channel_allowlist=SimpleNamespace(ids=frozenset({3})),
            operational_staff_role_ids=(7,),
        ),
        snapshot=SimpleNamespace(features={"arrest_report_lookup": SimpleNamespace(enabled=enabled)}),
    )


def test_command_requires_enabled_feature_configured_channel_and_operational_role() -> None:
    member = SimpleNamespace(roles=(SimpleNamespace(id=7),))
    assert _availability_denial(_bundle(), member, 3) is None  # type: ignore[arg-type]
    assert _availability_denial(_bundle(enabled=False), member, 3) is not None  # type: ignore[arg-type]
    wrong_channel = _availability_denial(_bundle(), member, 999)  # type: ignore[arg-type]
    assert wrong_channel is not None
    assert wrong_channel.description == (
        "Este canal no está habilitado para revisar reportes. "
        "Usa `/revisar-arresto` en uno de estos canales:\n"
        "• <#3>"
    )
    outsider = SimpleNamespace(roles=(SimpleNamespace(id=8),))
    assert _availability_denial(_bundle(), outsider, 3) is not None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_structured_service_stores_categories_in_order_and_deduplicates_binary() -> None:
    repository = FakeRepository(_submission())
    storage = MemoryObjectStorage()
    service = StructuredArrestReportService(repository, storage)  # type: ignore[arg-type]
    shared = StructuredReportFile("shared.png", "image/png", b"same")

    assets = await service.store_images(
        repository.submission,
        suspect_photos=(shared,),
        supporting_images=(shared, StructuredReportFile("report.png", "image/png", b"report")),
        max_file_bytes=100,
        max_total_bytes=1_000,
    )

    assert [asset.category for asset in assets] == [
        ArrestReportAssetCategory.SUSPECT_PHOTO,
        ArrestReportAssetCategory.SUPPORTING_IMAGE,
        ArrestReportAssetCategory.SUPPORTING_IMAGE,
    ]
    assert [asset.position for asset in assets] == [0, 0, 1]
    assert assets[0].object_key == assets[1].object_key
    assert len(storage.objects) == 2


@pytest.mark.asyncio
async def test_structured_service_rejects_non_images_and_invalid_group_sizes() -> None:
    repository = FakeRepository(_submission())
    service = StructuredArrestReportService(repository, MemoryObjectStorage())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="one or two"):
        await service.store_images(
            repository.submission,
            suspect_photos=(),
            supporting_images=(StructuredReportFile("report.png", "image/png", b"x"),),
            max_file_bytes=100,
            max_total_bytes=1_000,
        )
    with pytest.raises(ValueError, match="not supported"):
        await service.store_images(
            repository.submission,
            suspect_photos=(StructuredReportFile("photo.txt", "text/plain", b"x"),),
            supporting_images=(StructuredReportFile("report.png", "image/png", b"x"),),
            max_file_bytes=100,
            max_total_bytes=1_000,
        )


def test_generated_report_renderer_is_single_readable_png_with_semantic_styles() -> None:
    report = FakeArrestLookup().report
    report = report.__class__(
        report_id=8078,
        suspect_name="Dyson Sharpe",
        officer_name="Malcolm Crawford",
        description=(
            "Primera línea cortada por PCU cerca del límite\n"
            "pero perteneciente al mismo párrafo.\n\n"
            "Segundo párrafo.\n\n"
            "(( Primera referencia ))\n(( Segunda referencia ))"
        ),
        created_at=datetime(2026, 9, 15, 2, 42, tzinfo=UTC),
        updated_at=None,
        charges=(
            ArrestReportCharge(1, "CP-1", "Cargo grave", "GRAVE", 1, 100),
            ArrestReportCharge(2, "CP-2", "Cargo menor", "MENOR", 1, 100),
        ),
    )

    rendered = render_arrest_report_image(report)

    image = Image.open(io.BytesIO(rendered.payload))
    assert image.format == "PNG"
    assert image.width == 1000
    assert image.height > 700
    assert rendered.filename == "reporte-pcu-actual-8078.png"
    assert _description_paragraphs(report.description) == (
        "Primera línea cortada por PCU cerca del límite pero perteneciente al mismo párrafo.",
        "Segundo párrafo.",
        "(( Primera referencia ))",
        "(( Segunda referencia ))",
    )
    assert _charge_badge("GRAVE") == ("Grave", "#C93B3B")
    assert _charge_badge("MENOR") == ("Menor", "#536B86")


@pytest.mark.asyncio
async def test_image_tools_are_categorized_idempotent_and_owner_scoped() -> None:
    lookup = FakeImageLookup()
    tools = {
        tool.name: tool  # type: ignore[attr-defined]
        for tool in build_agent_tools(
            ChargeCatalog.load_default(),
            ReportGuide.load_default(),
            access_level=AgentAccessLevel.REPORT_OWNER,
            report_image_lookup=lookup,
            report_channel_ids=frozenset({3}),
        )
    }
    context = _context(AgentAccessLevel.REPORT_OWNER)
    arguments = "{}"
    first = await tools["get_report_suspect_images"].on_invoke_tool(  # type: ignore[attr-defined]
        ToolContext(
            context=context,
            tool_name="get_report_suspect_images",
            tool_call_id="first",
            tool_arguments=arguments,
        ),
        arguments,
    )
    second = await tools["get_report_suspect_images"].on_invoke_tool(  # type: ignore[attr-defined]
        ToolContext(
            context=context,
            tool_name="get_report_suspect_images",
            tool_call_id="second",
            tool_arguments=arguments,
        ),
        arguments,
    )
    current_report_arguments = json.dumps({"report_id": 321})
    current_report = await tools["get_report_suspect_images"].on_invoke_tool(  # type: ignore[attr-defined]
        ToolContext(
            context=context,
            tool_name="get_report_suspect_images",
            tool_call_id="current-report",
            tool_arguments=current_report_arguments,
        ),
        current_report_arguments,
    )
    forbidden_arguments = json.dumps({"report_id": 999})
    forbidden = await tools["get_report_supporting_images"].on_invoke_tool(  # type: ignore[attr-defined]
        ToolContext(
            context=context,
            tool_name="get_report_supporting_images",
            tool_call_id="forbidden",
            tool_arguments=forbidden_arguments,
        ),
        forbidden_arguments,
    )

    assert [type(item) for item in first] == [ToolOutputText, ToolOutputImage]
    first_metadata = json.loads(first[0].text)
    assert first_metadata["category"] == "suspect_photo"
    assert first_metadata["status"] == "images_loaded"
    assert first_metadata["image_presence"] == "present"
    assert first_metadata["required_for_review"] is False
    assert first_metadata["absence_affects_verdict"] is False
    assert [type(item) for item in second] == [ToolOutputText]
    assert json.loads(second[0].text)["skipped"][0]["reason"] == "already_loaded"
    assert [type(item) for item in current_report] == [ToolOutputText]
    assert lookup.calls[-1] == (_SUBMISSION_ID, None, None, "suspect_photo")
    forbidden_metadata = json.loads(forbidden[0].text)
    assert forbidden_metadata["status"] == "report_outside_owner_scope"
    assert forbidden_metadata["image_presence"] == "unknown"
    assert forbidden_metadata["can_determine_image_presence"] is False
    assert "loaded" not in forbidden_metadata


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        ("not_found", "structured_report_not_found"),
        ("temporarily_unavailable", "image_lookup_unavailable"),
    ],
)
async def test_image_tool_errors_do_not_claim_images_are_absent(
    error: str,
    expected_status: str,
) -> None:
    lookup = FakeImageLookup(error=error)
    tools = {
        tool.name: tool  # type: ignore[attr-defined]
        for tool in build_agent_tools(
            ChargeCatalog.load_default(),
            ReportGuide.load_default(),
            access_level=AgentAccessLevel.REPORT_OWNER,
            report_image_lookup=lookup,
            report_channel_ids=frozenset({3}),
        )
    }
    result = await tools["get_report_suspect_images"].on_invoke_tool(  # type: ignore[attr-defined]
        ToolContext(
            context=_context(AgentAccessLevel.REPORT_OWNER),
            tool_name="get_report_suspect_images",
            tool_call_id="lookup-error",
            tool_arguments="{}",
        ),
        "{}",
    )

    metadata = json.loads(result[0].text)
    assert metadata["ok"] is False
    assert metadata["status"] == expected_status
    assert metadata["image_presence"] == "unknown"
    assert metadata["can_determine_image_presence"] is False
    assert metadata["required_for_review"] is False
    assert metadata["absence_affects_verdict"] is False
    assert "loaded" not in metadata


@pytest.mark.asyncio
async def test_image_tool_reports_a_verified_empty_category() -> None:
    lookup = FakeImageLookup(empty=True)
    tools = {
        tool.name: tool  # type: ignore[attr-defined]
        for tool in build_agent_tools(
            ChargeCatalog.load_default(),
            ReportGuide.load_default(),
            access_level=AgentAccessLevel.REPORT_OWNER,
            report_image_lookup=lookup,
            report_channel_ids=frozenset({3}),
        )
    }
    result = await tools["get_report_suspect_images"].on_invoke_tool(  # type: ignore[attr-defined]
        ToolContext(
            context=_context(AgentAccessLevel.REPORT_OWNER),
            tool_name="get_report_suspect_images",
            tool_call_id="empty-category",
            tool_arguments="{}",
        ),
        "{}",
    )

    metadata = json.loads(result[0].text)
    assert metadata["ok"] is True
    assert metadata["status"] == "no_images_in_category"
    assert metadata["image_presence"] == "absent"
    assert metadata["can_determine_image_presence"] is True
    assert metadata["required_for_review"] is False
    assert metadata["absence_affects_verdict"] is False


@pytest.mark.asyncio
async def test_preloaded_report_detail_avoids_second_sarp_request() -> None:
    lookup = FakeArrestLookup()
    tools = {
        tool.name: tool  # type: ignore[attr-defined]
        for tool in build_agent_tools(
            ChargeCatalog.load_default(),
            ReportGuide.load_default(),
            access_level=AgentAccessLevel.REPORT_OWNER,
            arrest_report_lookup=lookup,  # type: ignore[arg-type]
            report_channel_ids=frozenset({3}),
        )
    }
    context = _context(AgentAccessLevel.REPORT_OWNER)
    context.preloaded_arrest_report = ArrestReportDetail(
        report_id=321,
        suspect_name="Andy Quintero",
        officer_name="Charles Harrington",
        description="Yo, Charles Harrington, procedí al arresto.",
        created_at=datetime.now(UTC),
        updated_at=None,
        charges=(
            ArrestReportCharge(
                charge_id=1,
                code="1.1",
                name="Cargo",
                charge_type="NO GRAVE",
                hours=1,
                fine=100,
            ),
        ),
    )

    result = await tools["get_current_arrest_report"].on_invoke_tool(  # type: ignore[attr-defined]
        ToolContext(
            context=context,
            tool_name="get_current_arrest_report",
            tool_call_id="current",
            tool_arguments="{}",
        ),
        "{}",
    )

    assert result["report"]["report_id"] == 321
    assert lookup.current_calls == 0


@pytest.mark.asyncio
async def test_owner_without_report_id_can_only_resolve_a_candidate_from_its_submission() -> None:
    lookup = FakeArrestLookup()
    tools = {
        tool.name: tool  # type: ignore[attr-defined]
        for tool in build_agent_tools(
            ChargeCatalog.load_default(),
            ReportGuide.load_default(),
            access_level=AgentAccessLevel.REPORT_OWNER,
            arrest_report_lookup=lookup,  # type: ignore[arg-type]
            report_channel_ids=frozenset({3}),
        )
    }
    context = _context(AgentAccessLevel.REPORT_OWNER)
    context.case = context.case.model_copy(update={"structured_report_id": None})

    search_arguments = json.dumps({"suspect_name": "Another Person"})
    search = await tools["search_arrest_reports"].on_invoke_tool(  # type: ignore[attr-defined]
        ToolContext(
            context=context,
            tool_name="search_arrest_reports",
            tool_call_id="search",
            tool_arguments=search_arguments,
        ),
        search_arguments,
    )
    outside_arguments = json.dumps({"report_id": 999})
    outside = await tools["get_arrest_report"].on_invoke_tool(  # type: ignore[attr-defined]
        ToolContext(
            context=context,
            tool_name="get_arrest_report",
            tool_call_id="outside",
            tool_arguments=outside_arguments,
        ),
        outside_arguments,
    )
    detail_arguments = json.dumps({"report_id": 321})
    detail = await tools["get_arrest_report"].on_invoke_tool(  # type: ignore[attr-defined]
        ToolContext(
            context=context,
            tool_name="get_arrest_report",
            tool_call_id="detail",
            tool_arguments=detail_arguments,
        ),
        detail_arguments,
    )

    assert search["status"] == "matched"
    assert lookup.searches[0].suspect_name == "Andy Quintero"
    assert outside["error"] == "report_outside_owner_scope"
    assert detail["ok"] is True
    assert detail["bound_to_current_thread"] is True
    assert lookup.binds[0]["report_id"] == 321


@pytest.mark.asyncio
async def test_modal_limits_and_submission_card_keep_the_two_galleries_separate() -> None:
    modal = StructuredArrestReportModal(
        cog=SimpleNamespace(),  # type: ignore[arg-type]
        requester_id=1,
        guild_id=2,
        channel_id=3,
    )
    assert modal.suspect_photos.min_values == 1
    assert modal.suspect_photos.max_values == 2
    assert modal.suspect_photos.required is True
    assert modal.supporting_images.min_values == 1
    assert modal.supporting_images.max_values == 8
    assert modal.supporting_images.required is True
    assert modal.report_id.required is True

    generated_modal = StructuredArrestReportModal(
        cog=SimpleNamespace(),  # type: ignore[arg-type]
        requester_id=1,
        guild_id=2,
        channel_id=3,
        generated_report_image=True,
    )
    assert generated_modal.supporting_images.min_values == 0
    assert generated_modal.supporting_images.max_values == 7
    assert generated_modal.supporting_images.required is False

    files, suspect_names, supporting_names = build_discord_files(
        (StructuredReportFile("photo.png", "image/png", b"a"),),
        (StructuredReportFile("photo.png", "image/png", b"b"),),
    )
    view = build_submission_view(
        _submission(),
        suspect_filenames=suspect_names,
        supporting_filenames=supporting_names,
    )

    assert len(files) == 2
    assert suspect_names == ("photo.png",)
    assert supporting_names == ("2-photo.png",)
    assert isinstance(view, discord.ui.DesignerView)
    container = view.children[0]
    assert isinstance(container, discord.ui.Container)
    assert container.color == discord.Color.gold()
    author_header = container.items[0]
    assert isinstance(author_header, discord.ui.TextDisplay)
    assert author_header.content.startswith("## 📋 Reporte de arresto · #321\n")
    assert f"**Autor:** <@{_submission().owner_user_id}>" in author_header.content
    assert "**Sospechoso:** Andy Quintero" in author_header.content
    assert "Presentado por" not in author_header.content
    assert _thread_name("Andy Quintero", 321) == "Andy Quintero · #321"
    assert _thread_name("Andy Quintero", None) == "Andy Quintero"

    generated = build_submission_view(
        _submission(),
        suspect_filenames=suspect_names,
        supporting_filenames=supporting_names,
        report_filename="reporte-pcu-actual-321.png",
        report_refreshed_at=datetime(2026, 9, 17, tzinfo=UTC),
    )
    generated_container = generated.children[0]
    assert isinstance(generated_container, discord.ui.Container)
    generated_text = [
        item.content for item in generated_container.items if isinstance(item, discord.ui.TextDisplay)
    ]
    assert "### 📄 Reporte de arresto" in generated_text
    assert "### 📎 Respaldos adicionales" in generated_text
    assert any("Actualizado durante la última revisión" in item for item in generated_text)
    assert sum(isinstance(item, discord.ui.MediaGallery) for item in generated_container.items) == 3

    without_id = build_submission_view(
        _submission(None),
        suspect_filenames=suspect_names,
        supporting_filenames=supporting_names,
    )
    container = without_id.children[0]
    assert isinstance(container, discord.ui.Container)
    header = container.items[0]
    assert isinstance(header, discord.ui.TextDisplay)
    assert header.content.startswith("## 📋 Reporte de arresto\n")
    assert "#None" not in header.content

    files, suspect_names, supporting_names = build_discord_files(
        (),
        (StructuredReportFile("report.png", "image/png", b"report"),),
    )
    without_suspect_photos = build_submission_view(
        _submission(),
        suspect_filenames=suspect_names,
        supporting_filenames=supporting_names,
    )
    container = without_suspect_photos.children[0]
    assert isinstance(container, discord.ui.Container)
    assert len(files) == 1
    assert suspect_names == ()
    assert supporting_names == ("report.png",)
    assert sum(isinstance(item, discord.ui.MediaGallery) for item in container.items) == 1
    assert any(
        isinstance(item, discord.ui.TextDisplay) and "No se adjuntaron fotografías" in item.content
        for item in container.items
    )


@pytest.mark.asyncio
async def test_snapshot_manager_restores_persisted_assets_and_preserves_verdict_color() -> None:
    submission = _submission().model_copy(
        update={
            "assets": (
                ArrestReportAsset(
                    id=uuid.uuid4(),
                    submission_id=_SUBMISSION_ID,
                    category=ArrestReportAssetCategory.SUSPECT_PHOTO,
                    position=0,
                    object_key="suspect",
                    sha256="a" * 64,
                    filename="suspect.png",
                    content_type="image/png",
                    size_bytes=10,
                    created_at=datetime.now(UTC),
                ),
                ArrestReportAsset(
                    id=uuid.uuid4(),
                    submission_id=_SUBMISSION_ID,
                    category=ArrestReportAssetCategory.SUPPORTING_IMAGE,
                    position=0,
                    object_key="support",
                    sha256="b" * 64,
                    filename="support.png",
                    content_type="image/png",
                    size_bytes=10,
                    created_at=datetime.now(UTC),
                ),
            )
        }
    )

    class Repository:
        async def get_by_root(self, guild_id: int, root_message_id: int) -> ArrestReportSubmission:
            assert (guild_id, root_message_id) == (1, 4)
            return submission

    class Provider:
        async def cached(self, guild_id: int) -> SimpleNamespace:
            assert guild_id == 1
            return SimpleNamespace(
                settings=SimpleNamespace(
                    arrest_report_attachment_mode=ArrestReportAttachmentMode.GENERATED,
                    max_file_bytes=10_000_000,
                ),
                service=SimpleNamespace(arrest_report_lookup=None),
            )

    class Storage:
        async def get(self, object_key: str) -> bytes:
            return {"suspect": b"suspect", "support": b"support"}[object_key]

    class Root:
        id = 4
        guild = SimpleNamespace(id=1)
        attachments: list[object] = []

        def __init__(self) -> None:
            self.edits: list[dict[str, Any]] = []

        async def edit(self, **values: Any) -> None:
            self.edits.append(values)

    report = FakeArrestLookup().report
    root = Root()
    manager = StructuredReportSnapshotManager(  # type: ignore[arg-type]
        Provider(),
        SimpleNamespace(repository=Repository(), storage=Storage()),
    )

    refreshed = await manager.refresh(  # type: ignore[arg-type]
        root,
        report,
        ReviewVerdict.APPROVED,
    )

    assert refreshed is True
    assert len(root.edits) == 1
    edit = root.edits[0]
    assert edit["attachments"] == []
    assert [file.filename for file in edit["files"]] == [
        "suspect.png",
        "support.png",
        "reporte-pcu-actual-321.png",
    ]
    container = edit["view"].children[0]
    assert isinstance(container, discord.ui.Container)
    assert container.color == discord.Color.green()
    assert sum(isinstance(item, discord.ui.MediaGallery) for item in container.items) == 3


@pytest.mark.parametrize(
    (
        "suspect_name",
        "report_id_text",
        "expected_detail_lookups",
        "expected_report_id",
        "expected_thread_name",
        "expected_error",
    ),
    (
        ("Andy Quintero", "", [], None, None, "usando solo números"),
        ("Andy Quintero", "not-a-number", [], None, None, "usando solo números"),
        ("Andy Quintero", "0", [], None, None, "mayor que cero"),
        ("Andy Quintero", "999", [999], None, None, "No existe un reporte"),
        ("Andi Quintero", "321", [321], None, None, "corresponde a Andy Quintero"),
        ("Andy Quintero", "321", [321], 321, "Andy Quintero · #321", None),
    ),
)
@pytest.mark.asyncio
async def test_submission_requires_an_existing_matching_arrest_id(
    monkeypatch,
    suspect_name: str,
    report_id_text: str,
    expected_detail_lookups: list[int],
    expected_report_id: int | None,
    expected_thread_name: str | None,
    expected_error: str | None,
) -> None:
    class Member:
        def __init__(self) -> None:
            self.id = 6
            self.roles = (SimpleNamespace(id=7),)
            self.display_name = "Charles Harrington"
            self.display_avatar = SimpleNamespace(url="https://cdn.discord.test/avatar.png")

    class Attachment:
        filename = "photo.png"
        content_type = "image/png"
        size = 3

        async def read(self) -> bytes:
            return b"png"

    class Lookup(FakeArrestLookup):
        def __init__(self) -> None:
            super().__init__()
            self.details: list[int] = []

        async def get(self, report_id: int) -> ArrestReportDetailResult:
            self.details.append(report_id)
            return await super().get(report_id)

    class Repository:
        def __init__(self) -> None:
            self.submission = _submission(expected_report_id).model_copy(
                update={
                    "interaction_id": 44,
                    "root_message_id": None,
                    "thread_id": None,
                    "publication_status": PublicationStatus.PUBLISHING,
                    "suspect_name": suspect_name,
                }
            )
            self.report_lookups: list[int] = []

        async def get_by_interaction(self, interaction_id: int) -> None:
            assert interaction_id == 44
            return None

        async def get_by_report(self, guild_id: int, report_id: int) -> None:
            self.report_lookups.append(report_id)
            return None

        async def reserve(self, **values: Any) -> tuple[ArrestReportSubmission, bool]:
            assert values["sarp_arrest_id"] == expected_report_id
            return self.submission, True

        async def mark_published(self, submission_id: uuid.UUID, **values: Any) -> ArrestReportSubmission:
            self.submission = self.submission.model_copy(
                update={
                    "root_message_id": values["root_message_id"],
                    "thread_id": values["thread_id"],
                    "publication_status": PublicationStatus.PUBLISHED,
                }
            )
            return self.submission

    class Service:
        def __init__(self, repository: Repository) -> None:
            self.repository = repository

        async def store_images(self, *_args: Any, **_values: Any) -> tuple[()]:
            return ()

    class Root:
        id = 4
        jump_url = "https://discord.test/report"

        async def create_thread(self, **values: Any) -> SimpleNamespace:
            assert expected_thread_name is not None
            assert values["name"] == expected_thread_name
            return SimpleNamespace(id=4, parent=channel)

    class Channel:
        id = 3

    class Response:
        def __init__(self) -> None:
            self.deferred = False
            self.messages: list[dict[str, Any]] = []

        async def send_message(self, **values: Any) -> None:
            self.messages.append(values)

        async def defer(self, *, ephemeral: bool, invisible: bool) -> None:
            assert ephemeral is False
            assert invisible is False
            self.deferred = True

    class Followup:
        def __init__(self) -> None:
            self.messages: list[dict[str, Any]] = []

        async def send(self, **values: Any) -> None:
            self.messages.append(values)

    class ReviewCog:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def run_structured_initial_review(self, **values: Any) -> None:
            self.calls.append(values)

    lookup = Lookup()
    repository = Repository()
    service = Service(repository)
    review_cog = ReviewCog()
    bundle = _bundle()
    bundle.settings.max_file_bytes = 10_000_000
    bundle.settings.max_total_file_bytes = 20_000_000
    bundle.settings.thread_auto_archive_minutes = 1440
    bundle.service = SimpleNamespace(arrest_report_lookup=lookup)

    class Provider:
        async def cached(self, guild_id: int) -> object:
            assert guild_id == 1
            return bundle

    channel = Channel()
    response = Response()
    followup = Followup()
    cleanup_calls = 0
    edits: list[dict[str, Any]] = []

    async def edit_original_response(**values: Any) -> Root:
        edits.append(values)
        return Root()

    async def delete_original_response() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1

    interaction = SimpleNamespace(
        id=44,
        user=Member(),
        guild=SimpleNamespace(id=1),
        channel=channel,
        response=response,
        followup=followup,
        edit_original_response=edit_original_response,
        delete_original_response=delete_original_response,
    )
    monkeypatch.setattr(discord, "Member", Member)
    cog = StructuredArrestReportCog(  # type: ignore[arg-type]
        Provider(),
        service,
        review_cog,
    )

    await cog.publish(
        interaction,  # type: ignore[arg-type]
        requester_id=6,
        guild_id=1,
        channel_id=3,
        suspect_name=suspect_name,
        report_id_text=report_id_text,
        suspect_photos=(Attachment(),),  # type: ignore[arg-type]
        supporting_images=(
            () if expected_error is None else (Attachment(),)  # type: ignore[arg-type]
        ),
        generated_report_image=expected_error is None,
    )
    await asyncio.sleep(0)

    assert lookup.details == expected_detail_lookups
    if expected_error is not None:
        assert response.deferred is False
        assert len(response.messages) == 1
        assert response.messages[0]["ephemeral"] is True
        error_embed = response.messages[0]["embed"]
        assert isinstance(error_embed, discord.Embed)
        assert expected_error in (error_embed.description or "")
        assert lookup.binds == []
        assert edits == []
        assert review_cog.calls == []
        return

    assert response.deferred is True
    assert response.messages == []
    assert cleanup_calls == 0
    assert followup.messages == []
    assert len(lookup.binds) == 1
    assert repository.report_lookups == []
    assert len(edits) == 1
    assert isinstance(edits[0]["view"], discord.ui.DesignerView)
    assert len(edits[0]["files"]) == 2
    container = edits[0]["view"].children[0]
    assert isinstance(container, discord.ui.Container)
    assert any(
        isinstance(item, discord.ui.TextDisplay) and item.content == "### 📄 Reporte de arresto"
        for item in container.items
    )
    allowed_mentions = edits[0]["allowed_mentions"]
    assert allowed_mentions.everyone is False
    assert allowed_mentions.users is False
    assert allowed_mentions.roles is False
    assert review_cog.calls[0]["arrest_report"] is not None
    assert review_cog.calls[0]["report_snapshot_current"] is True


def test_submission_schema_allows_reusing_a_sarp_report_id() -> None:
    report_index = next(
        index
        for index in arrest_report_submissions.indexes
        if index.name == "ix_arrest_submission_guild_report"
    )
    assert report_index.unique is not True
    assert [column.name for column in report_index.columns] == ["guild_id", "sarp_arrest_id"]
    assert all(
        constraint.name != "uq_arrest_submission_guild_report"
        for constraint in arrest_report_submissions.constraints
    )


@pytest.mark.asyncio
async def test_failed_publication_removes_public_acknowledgement_before_private_error() -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    class Followup:
        async def send(self, **values: Any) -> None:
            events.append(("followup", values))

    async def delete_original_response() -> None:
        events.append(("delete", {}))

    interaction = SimpleNamespace(
        delete_original_response=delete_original_response,
        followup=Followup(),
    )
    embed = discord.Embed(title="Error")

    await _replace_deferred_with_ephemeral_error(  # type: ignore[arg-type]
        interaction,
        embed,
        guild_id=1,
        user_id=6,
    )

    assert [event for event, _values in events] == ["delete", "followup"]
    assert events[1][1]["embed"] is embed
    assert events[1][1]["ephemeral"] is True
