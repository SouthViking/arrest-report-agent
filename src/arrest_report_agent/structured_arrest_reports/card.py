"""Components V2 rendering for structured arrest-report cards."""

from __future__ import annotations

from datetime import datetime
from typing import cast

import discord

from arrest_report_agent.domain import ReviewVerdict

from .models import ArrestReportSubmission


def build_submission_view(
    submission: ArrestReportSubmission,
    *,
    suspect_filenames: tuple[str, ...],
    supporting_filenames: tuple[str, ...],
    report_filename: str | None = None,
    report_refreshed_at: datetime | None = None,
    color: discord.Color | None = None,
) -> discord.ui.DesignerView:
    """Build the stable report card while preserving categorized attachments."""

    suspect_content: (
        discord.ui.MediaGallery[discord.ui.DesignerView]
        | discord.ui.TextDisplay[discord.ui.DesignerView, discord.ui.DesignerModal]
    ) = (
        discord.ui.MediaGallery(
            *(
                discord.MediaGalleryItem(  # type: ignore[no-untyped-call]
                    f"attachment://{filename}",
                    description=f"Fotografía del sospechoso {index}",
                )
                for index, filename in enumerate(suspect_filenames, start=1)
            )
        )
        if suspect_filenames
        else discord.ui.TextDisplay("_No se adjuntaron fotografías del sospechoso._")
    )
    report_suffix = f" · #{submission.sarp_arrest_id}" if submission.sarp_arrest_id is not None else ""
    header_text = (
        f"## 📋 Reporte de arresto{report_suffix}\n"
        f"**Autor:** <@{submission.owner_user_id}>\n"
        f"**Sospechoso:** {submission.suspect_name}"
    )
    container: discord.ui.Container[discord.ui.DesignerView] = discord.ui.Container(
        discord.ui.TextDisplay(header_text),
        discord.ui.Separator(),
        discord.ui.TextDisplay("### 📸 Fotografías del sospechoso"),
        suspect_content,
        color=color or discord.Color.gold(),
    )
    if report_filename is not None:
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay("### 📄 Reporte de arresto"))
        container.add_item(
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(  # type: ignore[no-untyped-call]
                    f"attachment://{report_filename}",
                    description=f"Reporte de arresto #{submission.sarp_arrest_id}",
                )
            )
        )
        if report_refreshed_at is not None:
            timestamp = int(report_refreshed_at.timestamp())
            container.add_item(
                discord.ui.TextDisplay(f"Actualizado durante la última revisión · <t:{timestamp}:R>")
            )
    if supporting_filenames:
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.TextDisplay(
                "### 📎 Respaldos adicionales"
                if report_filename is not None
                else "### 📎 Reporte y respaldos"
            )
        )
        container.add_item(
            discord.ui.MediaGallery(
                *(
                    discord.MediaGalleryItem(  # type: ignore[no-untyped-call]
                        f"attachment://{filename}",
                        description=f"Respaldo del reporte {index}",
                    )
                    for index, filename in enumerate(supporting_filenames, start=1)
                )
            )
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
    containers = [item for item in view.children if isinstance(item, discord.ui.Container)]
    if len(containers) != 1:
        raise ValueError("Structured report root does not contain exactly one container")
    if not _has_submission_signature(containers[0]):
        raise ValueError("Discord message is not a structured arrest-report card")
    containers[0].color = review_card_color(verdict)
    return view


def is_submission_card(message: discord.Message) -> bool:
    """Recognize the bot-owned card without relying on its database mapping."""

    try:
        view = cast(
            discord.ui.DesignerView,
            discord.ui.DesignerView.from_message(message, timeout=None),
        )
    except (TypeError, ValueError):
        return False
    containers = [item for item in view.children if isinstance(item, discord.ui.Container)]
    return len(containers) == 1 and _has_submission_signature(containers[0])


def _has_submission_signature(
    container: discord.ui.Container[discord.ui.DesignerView],
) -> bool:
    text_items = [item.content for item in container.items if isinstance(item, discord.ui.TextDisplay)]
    gallery_count = sum(isinstance(item, discord.ui.MediaGallery) for item in container.items)
    return (
        bool(text_items)
        and text_items[0].startswith("## 📋 Reporte de arresto")
        and "**Autor:**" in text_items[0]
        and "**Sospechoso:**" in text_items[0]
        and gallery_count in {1, 2, 3}
    )
