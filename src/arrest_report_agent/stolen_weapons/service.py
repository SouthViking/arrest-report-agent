"""Validate image bytes and persist theft evidence in the shared private bucket."""

from __future__ import annotations

import asyncio
import io

from PIL import Image, UnidentifiedImageError

from arrest_report_agent.infrastructure.assets import ImmutableAssetStore, UploadedAsset
from arrest_report_agent.infrastructure.storage import ObjectStorage

from .models import MAX_EVIDENCE_IMAGES, StolenWeaponReport, TheftDetails
from .repository import StolenWeaponRepository

IMAGE_FORMATS = {
    "PNG": ("image/png", "png"),
    "JPEG": ("image/jpeg", "jpg"),
    "WEBP": ("image/webp", "webp"),
    "GIF": ("image/gif", "gif"),
}


def validate_images(
    files: tuple[UploadedAsset, ...],
    *,
    max_file_bytes: int,
    max_total_bytes: int,
) -> tuple[UploadedAsset, ...]:
    if not 1 <= len(files) <= MAX_EVIDENCE_IMAGES:
        raise ValueError("Adjunta entre 1 y 5 imágenes de evidencia.")
    if sum(len(item.payload) for item in files) > max_total_bytes:
        raise ValueError("Las evidencias superan el tamaño total permitido.")
    validated: list[UploadedAsset] = []
    for index, item in enumerate(files, 1):
        if not item.payload or len(item.payload) > max_file_bytes:
            raise ValueError("Una evidencia está vacía o supera el tamaño permitido por imagen.")
        try:
            with Image.open(io.BytesIO(item.payload)) as image:
                content_type, extension = IMAGE_FORMATS[image.format or ""]
                image.verify()
        except (OSError, ValueError, KeyError, UnidentifiedImageError, Image.DecompressionBombError) as error:
            raise ValueError("Las evidencias deben ser imágenes válidas PNG, JPEG, WebP o GIF.") from error
        validated.append(UploadedAsset(f"evidencia-{index}.{extension}", content_type, item.payload))
    return tuple(validated)


class StolenWeaponService:
    def __init__(self, repository: StolenWeaponRepository, storage: ObjectStorage) -> None:
        self.repository = repository
        self.storage = storage
        self.asset_store = ImmutableAssetStore(storage)

    async def register(
        self,
        *,
        submission_id: int,
        guild_id: int,
        source_channel_id: int,
        destination_channel_id: int,
        registered_by: int,
        details: TheftDetails,
        files: tuple[UploadedAsset, ...],
        max_file_bytes: int,
        max_total_bytes: int,
    ) -> StolenWeaponReport:
        validated = await asyncio.to_thread(
            validate_images,
            files,
            max_file_bytes=max_file_bytes,
            max_total_bytes=max_total_bytes,
        )
        stored = await self.asset_store.store(
            validated,
            namespace=f"stolen-weapons/{guild_id}",
            max_files=MAX_EVIDENCE_IMAGES,
            max_file_bytes=max_file_bytes,
            max_total_bytes=max_total_bytes,
            allowed_content_types=frozenset(mime for mime, _extension in IMAGE_FORMATS.values()),
        )
        return await self.repository.create(
            submission_id=submission_id,
            guild_id=guild_id,
            source_channel_id=source_channel_id,
            destination_channel_id=destination_channel_id,
            registered_by=registered_by,
            details=details,
            assets=stored,
        )
