"""Application service for validating and storing logbook attachments."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from arrest_report_agent.infrastructure.assets import (
    AssetLimitError,
    ImmutableAssetStore,
    UploadedAsset,
    safe_filename,
)
from arrest_report_agent.infrastructure.storage import ObjectStorage

from .models import (
    LogbookAsset,
    LogbookCategory,
    LogbookEntry,
    LogbookSummary,
    PendingLogbookAsset,
)
from .repository import LogbookRepository

MAX_ATTACHMENTS = 10
__all__ = [
    "LogbookFile",
    "LogbookFileLimitError",
    "LogbookService",
    "safe_filename",
]


@dataclass(frozen=True, slots=True)
class LogbookFile:
    filename: str
    content_type: str | None
    payload: bytes


class LogbookFileLimitError(AssetLimitError):
    """Raised when an attachment exceeds a configured process limit."""


class LogbookService:
    def __init__(self, repository: LogbookRepository, storage: ObjectStorage) -> None:
        self.repository = repository
        self.storage = storage
        self.asset_store = ImmutableAssetStore(storage)

    async def store_files(
        self,
        entry: LogbookEntry,
        files: tuple[LogbookFile, ...],
        *,
        max_file_bytes: int,
        max_total_bytes: int,
    ) -> tuple[LogbookAsset, ...]:
        try:
            stored = await self.asset_store.store(
                tuple(
                    UploadedAsset(
                        filename=item.filename,
                        content_type=item.content_type,
                        payload=item.payload,
                    )
                    for item in files
                ),
                namespace=f"logbook/{entry.guild_id}",
                max_files=MAX_ATTACHMENTS,
                max_file_bytes=max_file_bytes,
                max_total_bytes=max_total_bytes,
            )
        except AssetLimitError as error:
            raise LogbookFileLimitError(str(error)) from error
        pending = tuple(
            PendingLogbookAsset(
                object_key=item.object_key,
                sha256=item.sha256,
                filename=item.filename,
                content_type=item.content_type,
                size_bytes=item.size_bytes,
            )
            for item in stored
        )
        return await self.repository.replace_assets(entry.id, pending)

    async def summarize(
        self,
        *,
        guild_id: int,
        date_from: date,
        date_to: date,
        category: LogbookCategory | None,
        subtype: str | None,
    ) -> LogbookSummary:
        return await self.repository.summarize(
            guild_id=guild_id,
            date_from=date_from,
            date_to=date_to,
            category=category,
            subtype=subtype,
        )
