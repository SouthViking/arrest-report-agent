"""Build Discord attachments for persisted structured-report images."""

from __future__ import annotations

import io

import discord

from arrest_report_agent.infrastructure.assets import safe_filename

from .service import StructuredReportFile


def build_discord_files(
    suspect: tuple[StructuredReportFile, ...],
    supporting: tuple[StructuredReportFile, ...],
    *,
    reserved_names: frozenset[str] = frozenset(),
) -> tuple[tuple[discord.File, ...], tuple[str, ...], tuple[str, ...]]:
    """Build uniquely named Discord files in their display order."""

    used: set[str] = {name.casefold() for name in reserved_names}
    files: list[discord.File] = []
    names: list[str] = []
    for index, item in enumerate((*suspect, *supporting), start=1):
        filename = safe_filename(item.filename)
        if filename.casefold() in used:
            filename = f"{index}-{filename}"[:255]
        used.add(filename.casefold())
        names.append(filename)
        files.append(discord.File(io.BytesIO(item.payload), filename=filename))
    split = len(suspect)
    return tuple(files), tuple(names[:split]), tuple(names[split:])
