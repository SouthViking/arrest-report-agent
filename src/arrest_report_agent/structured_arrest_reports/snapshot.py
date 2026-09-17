"""Refresh the one current generated report image on a structured Discord card."""

from __future__ import annotations

import asyncio
import io
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import discord

from arrest_report_agent.application.ports import ArrestReportDetail
from arrest_report_agent.config import ArrestReportAttachmentMode
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.domain import ReviewVerdict
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider

from .card import build_submission_view, review_card_color
from .models import ArrestReportAssetCategory
from .render import is_generated_report_filename, render_arrest_report_image
from .service import StructuredArrestReportService

if TYPE_CHECKING:
    from arrest_report_agent.arrest_reports import SarpArrestReportLookup

LOGGER = logging.getLogger(__name__)
MAX_DISCORD_ATTACHMENTS = 10


class StructuredReportSnapshotManager:
    """Apply the current guild mode whenever a formal review finishes."""

    def __init__(
        self,
        provider: DatabaseConfigProvider,
        service: StructuredArrestReportService,
    ) -> None:
        self.provider = provider
        self.service = service

    async def refresh(
        self,
        root: discord.Message,
        report: ArrestReportDetail | None,
        verdict: ReviewVerdict,
    ) -> bool:
        guild = root.guild
        if guild is None:
            return False
        bundle = await self.provider.cached(guild.id)
        if bundle.settings.arrest_report_attachment_mode is not ArrestReportAttachmentMode.GENERATED:
            return False
        submission = await self.service.repository.get_by_root(guild.id, root.id)
        if submission is None or submission.sarp_arrest_id is None:
            return False

        current = report if report is not None and report.report_id == submission.sarp_arrest_id else None
        if current is None:
            lookup = cast("SarpArrestReportLookup | None", bundle.service.arrest_report_lookup)
            if lookup is None:
                return False
            result = await lookup.get(submission.sarp_arrest_id)
            if result.error is not None or result.report is None:
                log_event(
                    LOGGER,
                    "warning",
                    "structured_arrest_report_snapshot_lookup_failed",
                    guild_id=guild.id,
                    root_message_id=root.id,
                    sarp_arrest_id=submission.sarp_arrest_id,
                    lookup_error=result.error or "not_found",
                )
                return False
            current = result.report

        rendered = await asyncio.to_thread(render_arrest_report_image, current)
        if len(rendered.payload) > bundle.settings.max_file_bytes:
            log_event(
                LOGGER,
                "warning",
                "structured_arrest_report_snapshot_too_large",
                guild_id=guild.id,
                root_message_id=root.id,
                sarp_arrest_id=submission.sarp_arrest_id,
                size_bytes=len(rendered.payload),
                max_file_bytes=bundle.settings.max_file_bytes,
            )
            return False

        retained = tuple(
            attachment
            for attachment in root.attachments
            if not is_generated_report_filename(attachment.filename)
        )
        if len(retained) >= MAX_DISCORD_ATTACHMENTS:
            log_event(
                LOGGER,
                "warning",
                "structured_arrest_report_snapshot_attachment_limit",
                guild_id=guild.id,
                root_message_id=root.id,
                sarp_arrest_id=submission.sarp_arrest_id,
                retained_attachment_count=len(retained),
            )
            return False

        suspect_count = sum(
            asset.category is ArrestReportAssetCategory.SUSPECT_PHOTO for asset in submission.assets
        )
        suspect_names = tuple(attachment.filename for attachment in retained[:suspect_count])
        supporting_names = tuple(attachment.filename for attachment in retained[suspect_count:])
        refreshed_at = datetime.now(UTC)
        await root.edit(
            attachments=list(retained),
            files=[discord.File(io.BytesIO(rendered.payload), filename=rendered.filename)],
            view=build_submission_view(
                submission,
                suspect_filenames=suspect_names,
                supporting_filenames=supporting_names,
                report_filename=rendered.filename,
                report_refreshed_at=refreshed_at,
                color=review_card_color(verdict),
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        log_event(
            LOGGER,
            "info",
            "structured_arrest_report_snapshot_refreshed",
            guild_id=guild.id,
            root_message_id=root.id,
            sarp_arrest_id=submission.sarp_arrest_id,
            retained_attachment_count=len(retained),
            generated_size_bytes=len(rendered.payload),
            verdict=verdict.value,
        )
        return True

    async def refresh_safely(
        self,
        root: discord.Message,
        report: ArrestReportDetail | None,
        verdict: ReviewVerdict,
    ) -> bool:
        """Keep review publication successful when the optional card refresh fails."""

        try:
            return await self.refresh(root, report, verdict)
        except Exception as error:
            log_event(
                LOGGER,
                "warning",
                "structured_arrest_report_snapshot_refresh_failed",
                exc_info=error,
                guild_id=getattr(getattr(root, "guild", None), "id", "unknown"),
                root_message_id=root.id,
                **exception_fields(error),
            )
            return False
