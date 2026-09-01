from __future__ import annotations

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
    ReportImageAsset,
    ReportImageResult,
)
from arrest_report_agent.application.service_context import ReviewRunContext
from arrest_report_agent.application.tools import build_agent_tools
from arrest_report_agent.domain import AgentAccessLevel, InteractionMode, ReviewCase
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
    StructuredArrestReportModal,
    _availability_denial,
    _discord_files,
    _thread_name,
    build_submission_view,
)
from arrest_report_agent.structured_arrest_reports.service import StructuredReportFile


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
        self.calls: list[tuple[int | None, int | None, str]] = []

    async def get_images(
        self,
        *,
        guild_id: int,
        root_message_id: int | None,
        report_id: int | None,
        category: str,
    ) -> ReportImageResult:
        self.calls.append((root_message_id, report_id, category))
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

    async def get_current(self, **_values: Any) -> ArrestReportDetailResult:
        self.current_calls += 1
        return ArrestReportDetailResult(report=None, error="not_bound")


def _submission() -> ArrestReportSubmission:
    now = datetime.now(UTC)
    return ArrestReportSubmission(
        id=uuid.uuid4(),
        guild_id=1,
        interaction_id=2,
        owner_user_id=6,
        parent_channel_id=3,
        root_message_id=4,
        thread_id=4,
        sarp_arrest_id=321,
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
    assert _thread_name("Andy Quintero", 321) == "Revisión · Andy Quintero · #321"
