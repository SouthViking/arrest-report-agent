from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import discord
import pytest
from agents import ToolOutputImage, ToolOutputText
from agents.tool_context import ToolContext

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
from arrest_report_agent.domain import AgentAccessLevel, InteractionMode, ReviewCase
from arrest_report_agent.infrastructure.postgres.schema import arrest_report_submissions
from arrest_report_agent.infrastructure.storage import MemoryObjectStorage
from arrest_report_agent.knowledge import ChargeCatalog, ReportGuide
from arrest_report_agent.structured_arrest_reports import (
    ArrestReportAsset,
    ArrestReportAssetCategory,
    ArrestReportSubmission,
    InitialReviewStatus,
    PublicationStatus,
    StructuredArrestReportService,
)
from arrest_report_agent.structured_arrest_reports.discord import (
    StructuredArrestReportCog,
    StructuredArrestReportModal,
    _availability_denial,
    _discord_files,
    _replace_deferred_with_ephemeral_error,
    _thread_name,
    build_submission_view,
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
    def __init__(self) -> None:
        self.calls: list[tuple[uuid.UUID | None, int | None, int | None, str]] = []

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
        return ReportImageResult(
            report_id=321,
            owner_user_id=6,
            images=(
                ReportImageAsset(
                    asset_id=f"{category}-1",
                    filename="image.png",
                    content_type="image/png",
                    size_bytes=8,
                    position=0,
                    payload=b"png-data",
                ),
            ),
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
        snapshot=SimpleNamespace(
            features={"arrest_report_lookup": SimpleNamespace(enabled=enabled)}
        ),
    )


def test_command_requires_enabled_feature_configured_channel_and_operational_role() -> None:
    member = SimpleNamespace(roles=(SimpleNamespace(id=7),))
    assert _availability_denial(_bundle(), member, 3) is None  # type: ignore[arg-type]
    assert _availability_denial(_bundle(enabled=False), member, 3) is not None  # type: ignore[arg-type]
    assert _availability_denial(_bundle(), member, 999) is not None  # type: ignore[arg-type]
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
    assert json.loads(first[0].text)["category"] == "suspect_photo"
    assert [type(item) for item in second] == [ToolOutputText]
    assert json.loads(second[0].text)["skipped"][0]["reason"] == "already_loaded"
    assert json.loads(forbidden[0].text)["error"] == "report_outside_owner_scope"


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
    assert modal.supporting_images.min_values == 1
    assert modal.supporting_images.max_values == 8
    assert modal.report_id.required is True

    files, suspect_names, supporting_names = _discord_files(
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


@pytest.mark.parametrize(
    (
        "suspect_name",
        "report_id_text",
        "expected_detail_lookups",
        "expected_report_id",
        "expected_thread_name",
    ),
    (
        ("Andy Quintero", "", [], None, "Andy Quintero"),
        ("Andy Quintero", "not-a-number", [], None, "Andy Quintero"),
        ("Andy Quintero", "999", [999], None, "Andy Quintero"),
        ("Andi Quintero", "321", [321], None, "Andi Quintero"),
        ("Andy Quintero", "321", [321], 321, "Andy Quintero · #321"),
    ),
)
@pytest.mark.asyncio
async def test_submission_uses_valid_matching_id_and_falls_back_to_agent_discovery(
    monkeypatch,
    suspect_name: str,
    report_id_text: str,
    expected_detail_lookups: list[int],
    expected_report_id: int | None,
    expected_thread_name: str,
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
            assert values["name"] == expected_thread_name
            return SimpleNamespace(id=4, parent=channel)

    class Channel:
        id = 3

    class Response:
        def __init__(self) -> None:
            self.deferred = False

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
    bundle.settings.max_file_bytes = 100
    bundle.settings.max_total_file_bytes = 1_000
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
        supporting_images=(Attachment(),),  # type: ignore[arg-type]
    )
    await asyncio.sleep(0)

    assert response.deferred is True
    assert cleanup_calls == 0
    assert followup.messages == []
    assert lookup.details == expected_detail_lookups
    assert len(lookup.binds) == (1 if expected_report_id is not None else 0)
    assert repository.report_lookups == []
    assert len(edits) == 1
    assert isinstance(edits[0]["view"], discord.ui.DesignerView)
    assert len(edits[0]["files"]) == 2
    allowed_mentions = edits[0]["allowed_mentions"]
    assert allowed_mentions.everyone is False
    assert allowed_mentions.users is False
    assert allowed_mentions.roles is False
    assert (review_cog.calls[0]["arrest_report"] is not None) == (
        expected_report_id is not None
    )


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
