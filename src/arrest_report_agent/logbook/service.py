"""Application service for validating and storing logbook attachments."""

from __future__ import annotations

import hashlib
import mimetypes
import re
from dataclasses import dataclass
from datetime import date
from pathlib import PurePath

from arrest_report_agent.infrastructure.storage import ObjectStorage

from .models import (
    LogbookAsset,
    LogbookCategory,
    LogbookEntry,
    LogbookSummary,
    PendingLogbookAsset,
)
from .repository import LogbookRepository

SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._ -]+")
MAX_ATTACHMENTS = 10


@dataclass(frozen=True, slots=True)
class LogbookFile:
    filename: str
    content_type: str | None
    payload: bytes


class LogbookFileLimitError(ValueError):
    """Raised when an attachment exceeds a configured process limit."""


class LogbookService:
    def __init__(self, repository: LogbookRepository, storage: ObjectStorage) -> None:
        self.repository = repository
        self.storage = storage

    async def store_files(
        self,
        entry: LogbookEntry,
        files: tuple[LogbookFile, ...],
        *,
        max_file_bytes: int,
        max_total_bytes: int,
    ) -> tuple[LogbookAsset, ...]:
        if len(files) > MAX_ATTACHMENTS:
            raise LogbookFileLimitError(f"At most {MAX_ATTACHMENTS} attachments are supported")
        total = sum(len(file.payload) for file in files)
        if total > max_total_bytes:
            raise LogbookFileLimitError("Combined attachments exceed the configured limit")
        pending: list[PendingLogbookAsset] = []
        for item in files:
            if not item.payload:
                raise LogbookFileLimitError("Empty attachments are not supported")
            if len(item.payload) > max_file_bytes:
                raise LogbookFileLimitError("An attachment exceeds the configured limit")
            digest = hashlib.sha256(item.payload).hexdigest()
            filename = safe_filename(item.filename)
            extension = PurePath(filename).suffix.lower()[:12]
            content_type = (
                (item.content_type or "").strip()
                or mimetypes.guess_type(filename)[0]
                or "application/octet-stream"
            )[:100]
            key = f"logbook/{entry.guild_id}/{digest[:2]}/{digest}{extension}"
            if not await self.storage.exists(key):
                await self.storage.put(key, item.payload, content_type)
            pending.append(
                PendingLogbookAsset(
                    object_key=key,
                    sha256=digest,
                    filename=filename,
                    content_type=content_type,
                    size_bytes=len(item.payload),
                )
            )
        return await self.repository.replace_assets(entry.id, tuple(pending))

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


def safe_filename(value: str) -> str:
    normalized = SAFE_FILENAME.sub("_", PurePath(value).name).strip(" .")
    return (normalized or "adjunto")[:255]
