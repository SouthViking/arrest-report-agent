"""Small dependency-free OOXML writer for the faction report attachment."""

# ruff: noqa: E501

from __future__ import annotations

import io
import math
import zipfile
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from xml.sax.saxutils import escape

from .models import FactionReport

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""

_WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <bookViews><workbookView xWindow="0" yWindow="0" windowWidth="24000" windowHeight="14000"/></bookViews>
  <sheets>
    <sheet name="Banco" sheetId="1" r:id="rId1"/>
    <sheet name="Miembros activos" sheetId="2" r:id="rId2"/>
  </sheets>
  <calcPr calcId="191029"/>
</workbook>"""

_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <numFmts count="3">
    <numFmt numFmtId="164" formatCode="yyyy-mm-dd hh:mm"/>
    <numFmt numFmtId="165" formatCode="$#,##0;[Red]-$#,##0"/>
    <numFmt numFmtId="166" formatCode="0.0"/>
  </numFmts>
  <fonts count="4">
    <font><sz val="11"/><name val="Aptos"/><family val="2"/></font>
    <font><b/><color rgb="FFFFFFFF"/><sz val="16"/><name val="Aptos Display"/><family val="2"/></font>
    <font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Aptos"/><family val="2"/></font>
    <font><b/><color rgb="FF334155"/><sz val="10"/><name val="Aptos"/><family val="2"/></font>
  </fonts>
  <fills count="4">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF17365D"/><bgColor indexed="64"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFE2E8F0"/><bgColor indexed="64"/></patternFill></fill>
  </fills>
  <borders count="2">
    <border><left/><right/><top/><bottom/><diagonal/></border>
    <border><left/><right/><top/><bottom style="thin"><color rgb="FFCBD5E1"/></bottom><diagonal/></border>
  </borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="11">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"><alignment vertical="center"/></xf>
    <xf numFmtId="0" fontId="3" fillId="0" borderId="0" xfId="0" applyFont="1"/>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="2" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"><alignment vertical="center"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1"><alignment vertical="top"/></xf>
    <xf numFmtId="164" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"><alignment vertical="top"/></xf>
    <xf numFmtId="165" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"><alignment horizontal="right" vertical="top"/></xf>
    <xf numFmtId="166" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"><alignment horizontal="right" vertical="top"/></xf>
    <xf numFmtId="0" fontId="0" fillId="3" borderId="1" xfId="0" applyFill="1" applyBorder="1"><alignment vertical="top"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1"><alignment wrapText="1" vertical="top"/></xf>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def build_faction_report_workbook(
    report: FactionReport,
    *,
    organization_name: str,
) -> bytes:
    """Return a compact two-sheet XLSX containing only the requested IC data."""

    bank_rows: list[Sequence[object]] = [
        (entry.created_at, entry.character_name, entry.concept, entry.amount)
        for entry in report.bank_entries
    ]
    member_rows: list[Sequence[object]] = [
        (
            member.name,
            member.rank_name,
            member.salary,
            member.activity.value,
            round(member.faction_time_minutes / 60, 1),
        )
        for member in report.active_members
    ]
    bank_sheet = _sheet_xml(
        title="Registro bancario",
        organization_name=organization_name,
        descriptor_label="Período",
        descriptor_value=f"{report.date_from.isoformat()} — {report.date_to.isoformat()}",
        generated_at=report.generated_at,
        headers=("Fecha (UTC)", "Responsable", "Concepto", "Monto"),
        rows=bank_rows,
        column_widths=(20, 28, 78, 16),
        column_styles=(6, 5, 10, 7),
    )
    member_sheet = _sheet_xml(
        title="Miembros activos",
        organization_name=organization_name,
        descriptor_label="Criterio",
        descriptor_value="Última conexión hace 14 días o menos",
        generated_at=report.generated_at,
        headers=("Miembro", "Rango", "Salario", "Actividad", "Horas de servicio"),
        rows=member_rows,
        column_widths=(30, 34, 16, 18, 20),
        column_styles=(5, 5, 7, 9, 8),
    )
    core = _core_properties(report.generated_at)
    app = _app_properties()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in (
            ("[Content_Types].xml", _CONTENT_TYPES),
            ("_rels/.rels", _ROOT_RELS),
            ("docProps/core.xml", core),
            ("docProps/app.xml", app),
            ("xl/workbook.xml", _WORKBOOK),
            ("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS),
            ("xl/styles.xml", _STYLES),
            ("xl/worksheets/sheet1.xml", bank_sheet),
            ("xl/worksheets/sheet2.xml", member_sheet),
        ):
            archive.writestr(name, content)
    return output.getvalue()


def _sheet_xml(
    *,
    title: str,
    organization_name: str,
    descriptor_label: str,
    descriptor_value: str,
    generated_at: datetime,
    headers: Sequence[str],
    rows: Iterable[Sequence[object]],
    column_widths: Sequence[int],
    column_styles: Sequence[int],
) -> str:
    row_values = list(rows)
    last_column = _column_name(len(headers))
    last_row = 6 + len(row_values)
    cols = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(column_widths, start=1)
    )
    rendered_rows = [
        _row(1, [_text_cell("A1", title, 1)], height=28),
        _row(
            2,
            [
                _text_cell("A2", "Departamento", 2),
                _text_cell("B2", organization_name, 3),
            ],
        ),
        _row(
            3,
            [
                _text_cell("A3", descriptor_label, 2),
                _text_cell("B3", descriptor_value, 3),
            ],
        ),
        _row(
            4,
            [
                _text_cell("A4", "Generado", 2),
                _text_cell("B4", f"{_utc(generated_at).strftime('%Y-%m-%d %H:%M')} UTC", 3),
            ],
        ),
        _row(6, [_text_cell(f"{_column_name(i)}6", value, 4) for i, value in enumerate(headers, 1)], height=22),
    ]
    for row_index, values in enumerate(row_values, start=7):
        cells = [
            _value_cell(
                f"{_column_name(column_index)}{row_index}",
                value,
                column_styles[column_index - 1],
            )
            for column_index, value in enumerate(values, start=1)
        ]
        rendered_rows.append(_row(row_index, cells))
    merges = (
        f'<mergeCell ref="A1:{last_column}1"/>'
        f'<mergeCell ref="B2:{last_column}2"/>'
        f'<mergeCell ref="B3:{last_column}3"/>'
        f'<mergeCell ref="B4:{last_column}4"/>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="A1:{last_column}{max(last_row, 6)}"/>'
        '<sheetViews><sheetView showGridLines="0" workbookViewId="0">'
        '<pane ySplit="6" topLeftCell="A7" activePane="bottomLeft" state="frozen"/>'
        '</sheetView></sheetViews>'
        '<sheetFormatPr defaultRowHeight="18"/>'
        f'<cols>{cols}</cols><sheetData>{"".join(rendered_rows)}</sheetData>'
        f'<autoFilter ref="A6:{last_column}{max(last_row, 6)}"/>'
        f'<mergeCells count="4">{merges}</mergeCells>'
        '<pageMargins left="0.4" right="0.4" top="0.6" bottom="0.6" header="0.2" footer="0.2"/>'
        '<pageSetup orientation="landscape" fitToWidth="1" fitToHeight="0"/>'
        '</worksheet>'
    )


def _row(index: int, cells: Sequence[str], *, height: int | None = None) -> str:
    sizing = f' ht="{height}" customHeight="1"' if height is not None else ""
    return f'<row r="{index}"{sizing}>{"".join(cells)}</row>'


def _text_cell(reference: str, value: str, style: int) -> str:
    value = _safe_xml_text(value)
    preserved = ' xml:space="preserve"' if value != value.strip() else ""
    return (
        f'<c r="{reference}" s="{style}" t="inlineStr"><is>'
        f'<t{preserved}>{escape(value)}</t></is></c>'
    )


def _value_cell(reference: str, value: object, style: int) -> str:
    if isinstance(value, datetime):
        return f'<c r="{reference}" s="{style}"><v>{_excel_serial(value)}</v></c>'
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
        return f'<c r="{reference}" s="{style}"><v>{value}</v></c>'
    return _text_cell(reference, "" if value is None else str(value), style)


def _excel_serial(value: datetime) -> str:
    epoch = datetime(1899, 12, 30)
    normalized = _utc(value).replace(tzinfo=None)
    return f"{(normalized - epoch).total_seconds() / 86_400:.10f}".rstrip("0").rstrip(".")


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _column_name(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _safe_xml_text(value: str) -> str:
    """Replace code points forbidden by XML 1.0 before serializing inline strings."""

    return "".join(character if _is_xml_character(ord(character)) else "�" for character in value)


def _is_xml_character(codepoint: int) -> bool:
    return (
        codepoint in {0x09, 0x0A, 0x0D}
        or 0x20 <= codepoint <= 0xD7FF
        or 0xE000 <= codepoint <= 0xFFFD
        or 0x10000 <= codepoint <= 0x10FFFF
    )


def _core_properties(generated_at: datetime) -> str:
    timestamp = _utc(generated_at).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:creator>arrest-report-agent</dc:creator>
  <cp:lastModifiedBy>arrest-report-agent</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:modified>
</cp:coreProperties>"""


def _app_properties() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>arrest-report-agent</Application>
  <DocSecurity>0</DocSecurity>
  <ScaleCrop>false</ScaleCrop>
  <HeadingPairs><vt:vector size="2" baseType="variant"><vt:variant><vt:lpstr>Worksheets</vt:lpstr></vt:variant><vt:variant><vt:i4>2</vt:i4></vt:variant></vt:vector></HeadingPairs>
  <TitlesOfParts><vt:vector size="2" baseType="lpstr"><vt:lpstr>Banco</vt:lpstr><vt:lpstr>Miembros activos</vt:lpstr></vt:vector></TitlesOfParts>
</Properties>"""
