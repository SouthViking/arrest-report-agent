"""Components V2 rendering for structured arrest-report cards."""

from __future__ import annotations

import discord

from arrest_report_agent.domain import ReviewVerdict

from .models import ArrestReportSubmission


def build_submission_view(
    submission: ArrestReportSubmission,
    *,
    suspect_filenames: tuple[str, ...],
    supporting_filenames: tuple[str, ...],
    presenter_name: str | None = None,
    presenter_avatar_url: str | None = None,
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
        f"-# Autor: <@{submission.owner_user_id}>\n"
        f"**Sospechoso:** {submission.suspect_name}"
    )
    header: discord.ui.ViewItem[discord.ui.DesignerView]
    if presenter_avatar_url:
        header = discord.ui.Section(
            discord.ui.TextDisplay(header_text),
            accessory=discord.ui.Thumbnail(
                presenter_avatar_url,
                description=f"Avatar de {presenter_name or 'quien presentó el reporte'}",
            ),
        )
    else:
        header = discord.ui.TextDisplay(header_text)
    container: discord.ui.Container[discord.ui.DesignerView] = discord.ui.Container(
        header,
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
