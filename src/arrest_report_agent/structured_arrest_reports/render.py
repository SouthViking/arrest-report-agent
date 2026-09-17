"""Deterministic single-image rendering for current SARP arrest reports."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from PIL import Image, ImageDraw, ImageFont

from arrest_report_agent.application.ports import ArrestReportCharge, ArrestReportDetail

IMAGE_WIDTH: Final = 1000
OUTER_PADDING: Final = 36
CONTENT_WIDTH: Final = IMAGE_WIDTH - (OUTER_PADDING * 2)
GENERATED_REPORT_PREFIX: Final = "reporte-pcu-actual-"

BACKGROUND = "#17191D"
PANEL = "#25282D"
NARRATIVE_PANEL = "#1F2226"
TEXT = "#F3F4F6"
MUTED = "#96A1B3"
GOLD = "#E7AD16"
RED = "#C93B3B"
MINOR = "#536B86"
ROW_BORDER = "#3A3F46"


@dataclass(frozen=True, slots=True)
class RenderedArrestReport:
    filename: str
    payload: bytes
    content_type: str = "image/png"


@dataclass(frozen=True, slots=True)
class _Fonts:
    title: ImageFont.FreeTypeFont | ImageFont.ImageFont
    label: ImageFont.FreeTypeFont | ImageFont.ImageFont
    value: ImageFont.FreeTypeFont | ImageFont.ImageFont
    heading: ImageFont.FreeTypeFont | ImageFont.ImageFont
    body: ImageFont.FreeTypeFont | ImageFont.ImageFont
    charge_code: ImageFont.FreeTypeFont | ImageFont.ImageFont
    charge_name: ImageFont.FreeTypeFont | ImageFont.ImageFont
    badge: ImageFont.FreeTypeFont | ImageFont.ImageFont


def generated_report_filename(report_id: int) -> str:
    return f"{GENERATED_REPORT_PREFIX}{report_id}.png"


def is_generated_report_filename(filename: str) -> bool:
    return bool(
        re.fullmatch(
            rf"{re.escape(GENERATED_REPORT_PREFIX)}[1-9][0-9]*\.png",
            Path(filename).name.casefold(),
        )
    )


def render_arrest_report_image(report: ArrestReportDetail) -> RenderedArrestReport:
    """Render one readable portrait PNG without generative or remote dependencies."""

    fonts = _load_fonts()
    measurement = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    narrative = tuple(
        _wrap_text(measurement, paragraph, fonts.body, CONTENT_WIDTH - 48)
        for paragraph in _description_paragraphs(report.description)
    )
    charge_lines = tuple(
        _wrap_text(measurement, charge.name, fonts.charge_name, 500) for charge in report.charges
    )
    height = _image_height(narrative, charge_lines)
    image = Image.new("RGB", (IMAGE_WIDTH, height), BACKGROUND)
    draw = ImageDraw.Draw(image)

    y = 28
    draw.text(
        (OUTER_PADDING, y),
        f"REPORTE DE ARRESTO · #{report.report_id}",
        font=fonts.title,
        fill=TEXT,
    )
    y += 54
    draw.rounded_rectangle(
        (OUTER_PADDING, y, IMAGE_WIDTH - OUTER_PADDING, y + 3),
        radius=2,
        fill=GOLD,
    )
    y += 24

    meta_height = 166
    draw.rounded_rectangle(
        (OUTER_PADDING, y, IMAGE_WIDTH - OUTER_PADDING, y + meta_height),
        radius=14,
        fill=PANEL,
    )
    column_two = 520
    _draw_meta_value(draw, OUTER_PADDING + 24, y + 22, "Sospechoso", report.suspect_name, fonts)
    _draw_meta_value(draw, column_two, y + 22, "Agente", report.officer_name, fonts)
    _draw_meta_value(
        draw,
        OUTER_PADDING + 24,
        y + 94,
        "Fecha del arresto",
        _spanish_datetime(report.created_at),
        fonts,
    )
    y += meta_height + 28

    draw.text((OUTER_PADDING, y), "Descripción del reporte", font=fonts.heading, fill=MUTED)
    y += 44
    narrative_height = _narrative_height(narrative)
    draw.rounded_rectangle(
        (OUTER_PADDING, y, IMAGE_WIDTH - OUTER_PADDING, y + narrative_height),
        radius=8,
        fill=NARRATIVE_PANEL,
        outline=GOLD,
        width=2,
    )
    text_y = y + 23
    line_height = 32
    for paragraph_index, lines in enumerate(narrative):
        for line in lines:
            draw.text((OUTER_PADDING + 24, text_y), line, font=fonts.body, fill=TEXT)
            text_y += line_height
        if paragraph_index < len(narrative) - 1:
            text_y += 18
    y += narrative_height + 28

    if report.charges:
        draw.text((OUTER_PADDING, y), "Cargos", font=fonts.heading, fill=MUTED)
        y += 44
        row_heights = tuple(_charge_row_height(lines) for lines in charge_lines)
        table_height = sum(row_heights)
        draw.rounded_rectangle(
            (OUTER_PADDING, y, IMAGE_WIDTH - OUTER_PADDING, y + table_height),
            radius=10,
            fill=PANEL,
            outline=ROW_BORDER,
            width=1,
        )
        row_y = y
        for index, (charge, lines, row_height) in enumerate(
            zip(report.charges, charge_lines, row_heights, strict=True)
        ):
            if index:
                draw.line(
                    (OUTER_PADDING, row_y, IMAGE_WIDTH - OUTER_PADDING, row_y),
                    fill=ROW_BORDER,
                    width=1,
                )
            _draw_charge_row(draw, charge, lines, row_y, row_height, fonts)
            row_y += row_height

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return RenderedArrestReport(
        filename=generated_report_filename(report.report_id),
        payload=output.getvalue(),
    )


def _load_fonts() -> _Fonts:
    return _Fonts(
        title=_font(34, bold=True),
        label=_font(19),
        value=_font(25, bold=True),
        heading=_font(26, bold=True),
        body=_font(24),
        charge_code=_font(23, bold=True),
        charge_name=_font(22),
        badge=_font(18, bold=True),
    )


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    filenames = (
        (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        )
        if bold
        else (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        )
    )
    for filename in filenames:
        if Path(filename).is_file():
            return ImageFont.truetype(filename, size=size)
    return ImageFont.load_default(size=size)


def _draw_meta_value(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    label: str,
    value: str,
    fonts: _Fonts,
) -> None:
    draw.text((x, y), label, font=fonts.label, fill=MUTED)
    draw.text((x, y + 28), value.replace("_", " "), font=fonts.value, fill=TEXT)


def _description_paragraphs(value: str) -> tuple[str, ...]:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ("Sin descripción registrada.",)
    paragraphs: list[str] = []
    for block in re.split(r"\n\s*\n", normalized):
        lines = tuple(line.strip() for line in block.splitlines() if line.strip())
        if not lines:
            continue
        if len(lines) > 1 and all(line.startswith("((") for line in lines):
            paragraphs.extend(lines)
        else:
            paragraphs.append(" ".join(lines))
    return tuple(paragraphs) or ("Sin descripción registrada.",)


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    value: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
) -> tuple[str, ...]:
    words = value.split()
    if not words:
        return ("",)
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return tuple(lines)


def _narrative_height(paragraphs: tuple[tuple[str, ...], ...]) -> int:
    line_count = sum(len(lines) for lines in paragraphs)
    gaps = max(0, len(paragraphs) - 1)
    return 46 + (line_count * 32) + (gaps * 18)


def _charge_row_height(lines: tuple[str, ...]) -> int:
    return max(68, 26 + (len(lines) * 29))


def _image_height(
    narrative: tuple[tuple[str, ...], ...],
    charge_lines: tuple[tuple[str, ...], ...],
) -> int:
    fixed = 28 + 54 + 24 + 166 + 28 + 44 + 28
    height = fixed + _narrative_height(narrative)
    if charge_lines:
        height += 44 + sum(_charge_row_height(lines) for lines in charge_lines)
    return height + 36


def _draw_charge_row(
    draw: ImageDraw.ImageDraw,
    charge: ArrestReportCharge,
    name_lines: tuple[str, ...],
    y: int,
    row_height: int,
    fonts: _Fonts,
) -> None:
    code = charge.code or "Sin código"
    center_y = y + (row_height // 2)
    draw.text((OUTER_PADDING + 24, center_y), code, font=fonts.charge_code, fill=TEXT, anchor="lm")
    divider_x = OUTER_PADDING + 220
    draw.line((divider_x, y + 18, divider_x, y + row_height - 18), fill=MUTED, width=2)
    line_height = 29
    name_y = center_y - ((len(name_lines) * line_height) // 2)
    for line in name_lines:
        draw.text((divider_x + 28, name_y), line, font=fonts.charge_name, fill=TEXT)
        name_y += line_height

    label, color = _charge_badge(charge.charge_type)
    badge_width = max(104, int(draw.textlength(label, font=fonts.badge)) + 34)
    badge_x = IMAGE_WIDTH - OUTER_PADDING - 24 - badge_width
    badge_y = center_y - 20
    draw.rounded_rectangle(
        (badge_x, badge_y, badge_x + badge_width, badge_y + 40),
        radius=18,
        fill=color,
    )
    draw.text(
        (badge_x + (badge_width // 2), center_y),
        label,
        font=fonts.badge,
        fill=TEXT,
        anchor="mm",
    )


def _charge_badge(value: str | None) -> tuple[str, str]:
    normalized = " ".join((value or "Sin tipo").replace("_", " ").split())
    if normalized.casefold() == "grave":
        return "Grave", RED
    return normalized.capitalize(), MINOR


def _spanish_datetime(value: object) -> str:
    if not hasattr(value, "day"):
        return str(value)
    months = (
        "ene",
        "feb",
        "mar",
        "abr",
        "may",
        "jun",
        "jul",
        "ago",
        "sept",
        "oct",
        "nov",
        "dic",
    )
    return (
        f"{value.day} {months[value.month - 1]} {value.year}, "  # type: ignore[attr-defined]
        f"{value.hour:02d}:{value.minute:02d}"  # type: ignore[attr-defined]
    )
