"""Application service for categorized report images."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from arrest_report_agent.application.ports import (
    ReportImageAsset,
    ReportImageCategory,
    ReportImageResult,
)
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.assets import ImmutableAssetStore, UploadedAsset
from arrest_report_agent.infrastructure.storage import ObjectStorage

from .models import (
    ArrestReportAsset,
    ArrestReportAssetCategory,
    ArrestReportSubmission,
    PendingArrestReportAsset,
)
from .repository import StructuredArrestReportRepository

ALLOWED_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StructuredReportFile:
    filename: str
    content_type: str | None
    payload: bytes


@dataclass(frozen=True, slots=True)
class LoadedReportAsset:
    asset: ArrestReportAsset
    payload: bytes


class StructuredArrestReportService:
    def __init__(
        self,
        repository: StructuredArrestReportRepository,
        storage: ObjectStorage,
    ) -> None:
        self.repository = repository
        self.storage = storage
        self.asset_store = ImmutableAssetStore(storage)

    async def store_images(
        self,
        submission: ArrestReportSubmission,
        *,
        suspect_photos: tuple[StructuredReportFile, ...],
        supporting_images: tuple[StructuredReportFile, ...],
        max_file_bytes: int,
        max_total_bytes: int,
    ) -> tuple[ArrestReportAsset, ...]:
        if not 1 <= len(suspect_photos) <= 2:
            raise ValueError("Suspect photographs must contain one or two images")
        if not 1 <= len(supporting_images) <= 8:
            raise ValueError("Supporting images must contain between one and eight images")
        all_files = (*suspect_photos, *supporting_images)
        stored = await self.asset_store.store(
            tuple(UploadedAsset(item.filename, item.content_type, item.payload) for item in all_files),
            namespace=f"arrest-reports/{submission.guild_id}/{submission.id}",
            max_files=10,
            max_file_bytes=max_file_bytes,
            max_total_bytes=max_total_bytes,
            allowed_content_types=ALLOWED_IMAGE_TYPES,
        )
        pending: list[PendingArrestReportAsset] = []
        for index, item in enumerate(stored):
            is_suspect = index < len(suspect_photos)
            pending.append(
                PendingArrestReportAsset(
                    category=(
                        ArrestReportAssetCategory.SUSPECT_PHOTO
                        if is_suspect
                        else ArrestReportAssetCategory.SUPPORTING_IMAGE
                    ),
                    position=index if is_suspect else index - len(suspect_photos),
                    object_key=item.object_key,
                    sha256=item.sha256,
                    filename=item.filename,
                    content_type=item.content_type,
                    size_bytes=item.size_bytes,
                )
            )
        return await self.repository.replace_assets(submission.id, tuple(pending))

    async def load_category(
        self,
        *,
        guild_id: int,
        category: ArrestReportAssetCategory,
        submission_id: UUID | None = None,
        root_message_id: int | None = None,
        report_id: int | None = None,
    ) -> tuple[ArrestReportSubmission | None, tuple[LoadedReportAsset, ...]]:
        identifiers = sum(
            value is not None for value in (submission_id, root_message_id, report_id)
        )
        if identifiers != 1:
            raise ValueError("Resolve a structured report by exactly one identifier")
        if submission_id is not None:
            submission = await self.repository.get_by_id(submission_id)
            if submission is not None and submission.guild_id != guild_id:
                submission = None
        elif root_message_id is not None:
            submission = await self.repository.get_by_root(guild_id, root_message_id)
        else:
            submission = await self.repository.get_by_report(guild_id, report_id or 0)
        if submission is None:
            return None, ()
        assets = tuple(asset for asset in submission.assets if asset.category is category)
        loaded: list[LoadedReportAsset] = []
        for asset in assets:
            loaded.append(
                LoadedReportAsset(
                    asset=asset,
                    payload=await self.storage.get(asset.object_key),
                )
            )
        return submission, tuple(loaded)

    async def get_images(
        self,
        *,
        guild_id: int,
        submission_id: UUID | None,
        root_message_id: int | None,
        report_id: int | None,
        category: ReportImageCategory,
    ) -> ReportImageResult:
        try:
            resolved_category = ArrestReportAssetCategory(category)
            submission, loaded = await self.load_category(
                guild_id=guild_id,
                submission_id=submission_id,
                root_message_id=root_message_id,
                report_id=report_id,
                category=resolved_category,
            )
        except Exception as error:
            log_event(
                LOGGER,
                "warning",
                "structured_arrest_report_images_unavailable",
                guild_id=guild_id,
                root_message_id=root_message_id,
                sarp_arrest_id=report_id,
                category=category,
                **exception_fields(error),
            )
            return ReportImageResult(
                report_id=report_id,
                owner_user_id=None,
                images=(),
                error="temporarily_unavailable",
            )
        if submission is None:
            return ReportImageResult(
                report_id=report_id,
                owner_user_id=None,
                images=(),
                error="not_found",
            )
        return ReportImageResult(
            report_id=submission.sarp_arrest_id,
            owner_user_id=submission.owner_user_id,
            images=tuple(
                ReportImageAsset(
                    asset_id=str(item.asset.id),
                    filename=item.asset.filename,
                    content_type=item.asset.content_type,
                    size_bytes=item.asset.size_bytes,
                    position=item.asset.position,
                    payload=item.payload,
                )
                for item in loaded
            ),
        )
