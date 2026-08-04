"""Classification of Discord attachments accepted by OpenAI file inputs."""

from __future__ import annotations

import mimetypes
from enum import StrEnum
from pathlib import PurePath


class AttachmentInputKind(StrEnum):
    """Responses API input representation for one Discord attachment."""

    IMAGE = "image"
    FILE = "file"


# Image types accepted by OpenAI vision inputs. Animated GIFs can still be rejected by
# the provider; that error is handled by the normal runner error boundary.
SUPPORTED_IMAGE_EXTENSIONS = frozenset({".gif", ".jpeg", ".jpg", ".png", ".webp"})
SUPPORTED_IMAGE_MIME_TYPES = frozenset(
    {"image/gif", "image/jpeg", "image/png", "image/webp"}
)

# OpenAI Responses API file-input extensions. Keeping the allowlist explicit prevents
# archives, executables, audio and video from reaching the runner accidentally.
SUPPORTED_FILE_EXTENSIONS = frozenset(
    {
        ".asm",
        ".bat",
        ".c",
        ".cc",
        ".conf",
        ".cpp",
        ".css",
        ".csv",
        ".cxx",
        ".def",
        ".dic",
        ".doc",
        ".docx",
        ".dot",
        ".eml",
        ".h",
        ".hh",
        ".htm",
        ".html",
        ".ics",
        ".ifb",
        ".iif",
        ".in",
        ".js",
        ".json",
        ".ksh",
        ".list",
        ".log",
        ".markdown",
        ".md",
        ".mht",
        ".mhtml",
        ".mime",
        ".mjs",
        ".nws",
        ".odt",
        ".pdf",
        ".pl",
        ".pot",
        ".ppa",
        ".pps",
        ".ppt",
        ".pptx",
        ".pwz",
        ".py",
        ".rst",
        ".rtf",
        ".s",
        ".sql",
        ".srt",
        ".text",
        ".tsv",
        ".txt",
        ".vcf",
        ".vtt",
        ".wiz",
        ".xla",
        ".xlb",
        ".xlc",
        ".xlm",
        ".xls",
        ".xlsx",
        ".xlt",
        ".xlw",
        ".xml",
    }
)

SUPPORTED_FILE_MIME_TYPES = frozenset(
    {
        "application/json",
        "application/msword",
        "application/pdf",
        "application/rtf",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "application/vnd.oasis.opendocument.text",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/xml",
        "text/csv",
        "text/html",
        "text/markdown",
        "text/plain",
        "text/rtf",
        "text/tab-separated-values",
        "text/vcard",
        "text/xml",
    }
)


def classify_attachment(
    filename: str,
    content_type: str | None,
) -> tuple[AttachmentInputKind | None, str | None]:
    """Return the OpenAI input kind and a normalized/guessed MIME type."""

    normalized_mime = _normalize_mime(content_type)
    guessed_mime = mimetypes.guess_type(filename)[0]
    resolved_mime = normalized_mime or guessed_mime
    extension = PurePath(filename).suffix.casefold()

    if extension in SUPPORTED_IMAGE_EXTENSIONS or resolved_mime in SUPPORTED_IMAGE_MIME_TYPES:
        return AttachmentInputKind.IMAGE, resolved_mime
    if extension in SUPPORTED_FILE_EXTENSIONS or resolved_mime in SUPPORTED_FILE_MIME_TYPES:
        return AttachmentInputKind.FILE, resolved_mime
    return None, resolved_mime


def _normalize_mime(content_type: str | None) -> str | None:
    if not content_type:
        return None
    normalized = content_type.partition(";")[0].strip().casefold()
    return normalized or None
