from __future__ import annotations

import pytest

from arrest_report_agent.infrastructure.discord.attachments import (
    AttachmentInputKind,
    classify_attachment,
)


@pytest.mark.parametrize(
    ("filename", "content_type", "expected"),
    [
        ("suspect.JPG", None, AttachmentInputKind.IMAGE),
        ("report.png", "image/png", AttachmentInputKind.IMAGE),
        ("report.txt", "text/plain; charset=utf-8", AttachmentInputKind.FILE),
        ("report.pdf", "application/octet-stream", AttachmentInputKind.FILE),
        ("report.docx", None, AttachmentInputKind.FILE),
        ("charges.xlsx", None, AttachmentInputKind.FILE),
        ("briefing.pptx", None, AttachmentInputKind.FILE),
        ("data.json", None, AttachmentInputKind.FILE),
        ("archive.zip", "application/zip", None),
        ("video.mp4", "video/mp4", None),
        ("audio.mp3", "audio/mpeg", None),
    ],
)
def test_classify_attachment_uses_supported_openai_input_types(
    filename: str,
    content_type: str | None,
    expected: AttachmentInputKind | None,
) -> None:
    kind, _ = classify_attachment(filename, content_type)

    assert kind is expected


def test_classify_attachment_normalizes_mime_parameters() -> None:
    kind, content_type = classify_attachment("notes", "text/plain; charset=UTF-8")

    assert kind is AttachmentInputKind.FILE
    assert content_type == "text/plain"
