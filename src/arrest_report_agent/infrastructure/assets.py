"""Shared validation and content-addressed storage for immutable uploaded assets."""

from __future__ import annotations

import hashlib
import mimetypes
import re
from dataclasses import dataclass
from pathlib import PurePath

from .storage import ObjectStorage

SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._ -]+")


class AssetLimitError(ValueError):
    """Raised when an uploaded asset collection exceeds a configured boundary."""


@dataclass(frozen=True, slots=True)
class UploadedAsset:
    filename: str
    content_type: str | None
    payload: bytes


@dataclass(frozen=True, slots=True)
class StoredAsset:
    object_key: str
    sha256: str
    filename: str
    content_type: str
    size_bytes: int


class ImmutableAssetStore:
    """Validate and store immutable files using deterministic content hashes."""

    def __init__(self, storage: ObjectStorage) -> None:
        self.storage = storage

    async def store(
        self,
        files: tuple[UploadedAsset, ...],
        *,
        namespace: str,
        max_files: int,
        max_file_bytes: int,
        max_total_bytes: int,
        allowed_content_types: frozenset[str] | None = None,
    ) -> tuple[StoredAsset, ...]:
        if len(files) > max_files:
            raise AssetLimitError(f"At most {max_files} attachments are supported")
        if sum(len(file.payload) for file in files) > max_total_bytes:
            raise AssetLimitError("Combined attachments exceed the configured limit")

        stored: list[StoredAsset] = []
        for item in files:
            if not item.payload:
                raise AssetLimitError("Empty attachments are not supported")
            if len(item.payload) > max_file_bytes:
                raise AssetLimitError("An attachment exceeds the configured limit")
            filename = safe_filename(item.filename)
            content_type = resolved_content_type(filename, item.content_type)
            if allowed_content_types is not None and content_type not in allowed_content_types:
                raise AssetLimitError("An attachment type is not supported")
            digest = hashlib.sha256(item.payload).hexdigest()
            extension = PurePath(filename).suffix.lower()[:12]
            key = f"{namespace.strip('/')}/{digest[:2]}/{digest}{extension}"
            if not await self.storage.exists(key):
                await self.storage.put(key, item.payload, content_type)
            stored.append(
                StoredAsset(
                    object_key=key,
                    sha256=digest,
                    filename=filename,
                    content_type=content_type,
                    size_bytes=len(item.payload),
                )
            )
        return tuple(stored)


def safe_filename(value: str) -> str:
    normalized = SAFE_FILENAME.sub("_", PurePath(value).name).strip(" .")
    return (normalized or "adjunto")[:255]


def resolved_content_type(filename: str, content_type: str | None) -> str:
    return (
        (content_type or "").partition(";")[0].strip().casefold()
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )[:100]
