"""Components V2 rendering for structured arrest-report cards."""

from __future__ import annotations

from typing import cast

import discord

from arrest_report_agent.domain import ReviewVerdict

from .models import ArrestReportSubmission


def build_submission_view(
    submission: ArrestReportSubmission,
    *,
    suspect_filenames: tuple[str, ...],
    supporting_filenames: tuple[str, ...],
    color: discord.Color | None = None,
) -> discord.ui.DesignerView:
    """Build the stable report card while preserving both categorized galleries."""

    suspect_gallery: discord.ui.MediaGallery[discord.ui.DesignerView] = discord.ui.MediaGallery(
        *(
            discord.MediaGalleryItem(  # type: ignore[no-untyped-call]
                f"attachment://{filename}",
                description=f"Fotografía del sospechoso {index}",
            )
            for index, filename in enumerate(suspect_filenames, start=1)
        )
    )
    supporting_gallery: discord.ui.MediaGallery[discord.ui.DesignerView] = discord.ui.MediaGallery(
        *(
            discord.MediaGalleryItem(  # type: ignore[no-untyped-call]
                f"attachment://{filename}",
                description=f"Respaldo del reporte {index}",
            )
            for index, filename in enumerate(supporting_filenames, start=1)
        )
    )
    report_suffix = (
        f" · #{submission.sarp_arrest_id}"
        if submission.sarp_arrest_id is not None
        else ""
    )
    header_text = (
        f"## 📋 Reporte de arresto{report_suffix}\n"
        f"**Autor:** <@{submission.owner_user_id}>\n"
        f"**Sospechoso:** {submission.suspect_name}"
    )
    container: discord.ui.Container[discord.ui.DesignerView] = discord.ui.Container(
        discord.ui.TextDisplay(header_text),
        discord.ui.Separator(),
        discord.ui.TextDisplay("### 📸 Fotografías del sospechoso"),
        suspect_gallery,
        discord.ui.Separator(),
        discord.ui.TextDisplay("### 📎 Reporte y respaldos"),
        supporting_gallery,
        color=color or discord.Color.gold(),
    )
    return discord.ui.DesignerView(container, timeout=None, store=False)


def review_card_color(verdict: ReviewVerdict) -> discord.Color:
    """Map a completed full-review verdict to one deterministic visual state."""

    if verdict is ReviewVerdict.APPROVED:
        return discord.Color.green()
    if verdict is ReviewVerdict.CORRECTIONS_REQUIRED:
        return discord.Color.red()
    return discord.Color.orange()


def recolor_submission_view(
    message: discord.Message,
    verdict: ReviewVerdict,
) -> discord.ui.DesignerView:
    """Preserve an existing Components V2 card while changing only its accent color."""

    view = cast(
        discord.ui.DesignerView,
        discord.ui.DesignerView.from_message(message, timeout=None),
    )
    containers = [
        item for item in view.children if isinstance(item, discord.ui.Container)
    ]
    if len(containers) != 1:
        raise ValueError("Structured report root does not contain exactly one container")
    containers[0].color = review_card_color(verdict)
    return view
