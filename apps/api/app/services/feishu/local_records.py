from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape
import io
import json
import zipfile

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Job, User, UserConfig
from app.services.feishu.local_writer import _local_record_path
from app.services.user_settings import load_user_settings

LOCAL_FEISHU_FIELD_ORDER = [
    "Trae Session ID",
    "轮次",
    "User Prompt",
    "任务类型",
    "业务领域",
    "修改范围",
    "任务是否完成",
    "产物及过程是否满意",
    "不满意原因",
    "github地址",
    "commit id",
    "分支/文件夹",
    "日志轨迹",
    "截图（userprompt附件/产物/运行结果/对话）",
]

LOCAL_FEISHU_EXPORT_METADATA_COLUMNS = [
    ("record_id", "记录ID"),
    ("created_at", "创建时间"),
    ("job_id", "Job ID"),
    ("round_id", "Round ID"),
    ("user_id", "用户ID"),
    ("source_path", "来源文件"),
    ("line_number", "行号"),
]

EXCEL_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_EXCEL_CELL_CHARS = 32767
EXCEL_TRUNCATED_SUFFIX = "\n[truncated: Excel cell limit]"


def list_local_feishu_records(db: Session, user: User, *, limit: int = 200) -> dict[str, Any]:
    limit = max(1, min(int(limit or 200), 1000))
    records = _read_records(_record_paths_for_user(db, user))
    job_ids = {item["job_id"] for item in records if item["job_id"]}
    owners = _job_owner_map(db, job_ids)

    visible: list[dict[str, Any]] = []
    for item in records:
        owner = owners.get(item["job_id"], {})
        if user.role != "admin" and item["job_id"] and owner.get("user_id") != user.id:
            continue
        if user.role != "admin" and not item["job_id"]:
            continue
        visible.append({**item, **owner})

    visible.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return {
        "fields": LOCAL_FEISHU_FIELD_ORDER,
        "records": visible[:limit],
        "paths": [str(path) for path in _record_paths_for_user(db, user)],
    }


def export_local_feishu_records_xlsx(records_data: dict[str, Any], *, keyword: str = "") -> bytes:
    fields = [str(item) for item in records_data.get("fields") or LOCAL_FEISHU_FIELD_ORDER]
    records = records_data.get("records") if isinstance(records_data.get("records"), list) else []
    records = _filter_records_by_keyword(records, keyword)
    headers = fields + [label for _key, label in LOCAL_FEISHU_EXPORT_METADATA_COLUMNS]
    rows = [headers]
    for record in records:
        record_fields = record.get("fields") if isinstance(record.get("fields"), dict) else {}
        row = [_stringify_export_value(record_fields.get(field)) for field in fields]
        row.extend(_stringify_export_value(record.get(key)) for key, _label in LOCAL_FEISHU_EXPORT_METADATA_COLUMNS)
        rows.append(row)
    return _build_xlsx(rows, sheet_name="本地记录")


def _filter_records_by_keyword(records: list[dict[str, Any]], keyword: str) -> list[dict[str, Any]]:
    needle = str(keyword or "").strip().lower()
    if not needle:
        return records
    result = []
    for record in records:
        haystack = json.dumps(
            {
                "record_id": record.get("record_id"),
                "created_at": record.get("created_at"),
                "fields": record.get("fields"),
                "metadata": record.get("metadata"),
                "job_id": record.get("job_id"),
                "round_id": record.get("round_id"),
            },
            ensure_ascii=False,
            default=str,
        ).lower()
        if needle in haystack:
            result.append(record)
    return result


def _build_xlsx(rows: list[list[str]], *, sheet_name: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as workbook:
        workbook.writestr("[Content_Types].xml", _content_types_xml())
        workbook.writestr("_rels/.rels", _root_relationships_xml())
        workbook.writestr("docProps/core.xml", _core_properties_xml())
        workbook.writestr("docProps/app.xml", _app_properties_xml())
        workbook.writestr("xl/workbook.xml", _workbook_xml(sheet_name))
        workbook.writestr("xl/_rels/workbook.xml.rels", _workbook_relationships_xml())
        workbook.writestr("xl/styles.xml", _styles_xml())
        workbook.writestr("xl/worksheets/sheet1.xml", _worksheet_xml(rows))
    return buffer.getvalue()


def _worksheet_xml(rows: list[list[str]]) -> str:
    row_count = max(1, len(rows))
    column_count = max((len(row) for row in rows), default=1)
    last_cell = f"{_excel_column_name(column_count)}{row_count}"
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        f'<dimension ref="A1:{last_cell}"/>',
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
        'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>',
        '<sheetFormatPr defaultRowHeight="15"/>',
        _columns_xml(column_count),
        "<sheetData>",
    ]
    for row_index, row in enumerate(rows, start=1):
        height = 24 if row_index == 1 else 54
        parts.append(f'<row r="{row_index}" ht="{height}" customHeight="1">')
        for column_index, value in enumerate(row, start=1):
            cell_ref = f"{_excel_column_name(column_index)}{row_index}"
            style = "1" if row_index == 1 else "0"
            parts.append(_inline_string_cell_xml(cell_ref, value, style=style))
        parts.append("</row>")
    parts.extend(
        [
            "</sheetData>",
            f'<autoFilter ref="A1:{last_cell}"/>',
            '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>',
            "</worksheet>",
        ]
    )
    return "".join(parts)


def _columns_xml(column_count: int) -> str:
    wide_columns = {3, 9, 13, 14}
    parts = ["<cols>"]
    for index in range(1, column_count + 1):
        width = 56 if index in wide_columns else 24
        parts.append(f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>')
    parts.append("</cols>")
    return "".join(parts)


def _inline_string_cell_xml(cell_ref: str, value: str, *, style: str) -> str:
    text = _excel_cell_text(value)
    return (
        f'<c r="{cell_ref}" t="inlineStr" s="{style}">'
        f'<is><t xml:space="preserve">{escape(text)}</t></is>'
        "</c>"
    )


def _excel_cell_text(value: str) -> str:
    text = _clean_xml_text(str(value or ""))
    if len(text) <= MAX_EXCEL_CELL_CHARS:
        return text
    keep = MAX_EXCEL_CELL_CHARS - len(EXCEL_TRUNCATED_SUFFIX)
    return f"{text[:keep]}{EXCEL_TRUNCATED_SUFFIX}"


def _clean_xml_text(value: str) -> str:
    return "".join(
        char
        for char in value
        if char in {"\t", "\n", "\r"} or ord(char) >= 0x20
    )


def _stringify_export_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(_stringify_export_value(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return str(value)


def _excel_column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name or "A"


def _content_types_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/docProps/core.xml" '
        'ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        "</Types>"
    )


def _root_relationships_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" '
        'Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" '
        'Target="docProps/app.xml"/>'
        "</Relationships>"
    )


def _workbook_xml(sheet_name: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<sheets>"
        f'<sheet name="{escape(sheet_name[:31])}" sheetId="1" r:id="rId1"/>'
        "</sheets>"
        "</workbook>"
    )


def _workbook_relationships_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
        "</Relationships>"
    )


def _styles_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="2">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" applyAlignment="1">'
        '<alignment vertical="top" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" applyFont="1" applyAlignment="1">'
        '<alignment vertical="center" wrapText="1"/></xf>'
        "</cellXfs>"
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        "</styleSheet>"
    )


def _core_properties_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        "<dc:title>AgentOps Local Feishu Records</dc:title>"
        "<dc:creator>AgentOps</dc:creator>"
        "</cp:coreProperties>"
    )


def _app_properties_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        "<Application>AgentOps</Application>"
        "</Properties>"
    )


def _record_paths_for_user(db: Session, user: User) -> list[Path]:
    configs: list[dict[str, Any]] = []
    if user.role == "admin":
        configs.append({})
        rows = db.scalars(select(UserConfig).where(UserConfig.category == "feishu")).all()
        configs.extend(row.data or {} for row in rows)
    else:
        configs.append(load_user_settings(db, user.id).get("feishu", {}))

    paths: list[Path] = []
    seen: set[str] = set()
    for config in configs:
        path = _local_record_path(config)
        key = str(path)
        if key not in seen:
            paths.append(path)
            seen.add(key)
    return paths


def _read_records(paths: list[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists() or not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            fields = raw.get("fields") if isinstance(raw.get("fields"), dict) else {}
            metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
            records.append(
                {
                    "record_id": str(raw.get("record_id") or f"{path.name}:{line_number}"),
                    "created_at": str(raw.get("created_at") or ""),
                    "fields": fields,
                    "metadata": metadata,
                    "job_id": str(metadata.get("job_id") or ""),
                    "round_id": str(metadata.get("round_id") or ""),
                    "source_path": str(path),
                    "line_number": line_number,
                }
            )
    return records


def _job_owner_map(db: Session, job_ids: set[str]) -> dict[str, dict[str, str]]:
    if not job_ids:
        return {}
    jobs = db.scalars(select(Job).where(Job.id.in_(job_ids))).all()
    return {job.id: {"user_id": job.user_id} for job in jobs}
