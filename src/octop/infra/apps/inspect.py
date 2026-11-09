"""Server-side inspection of spreadsheet files for light-app authoring.

The agent must not read a spreadsheet row by row: a ledger can hold tens of
thousands of rows, which would blow the context window and let the model
misread values. This module parses the file deterministically and returns a
compact report — sheet names, the header row, column statistics and a few
sample rows — so the model can propose a schema and ask the user to confirm it.

Everything here is pure: it takes bytes and returns a JSON-ready dict. The
caller (the ``app_inspect_file`` tool) handles workspace access and auth.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import PurePosixPath
from typing import Any

from pypinyin import Style, lazy_pinyin

from octop.infra.apps.pii import detect_header_pii, detect_text_pii
from octop.infra.errors import ErrorCode, OctopError

MAX_INSPECT_BYTES = 50 * 1024 * 1024
"""Hard cap on the file size we are willing to parse for a report."""

MAX_SCANNED_ROWS = 20_000
"""Rows scanned per sheet for statistics; beyond this we report a warning."""

MAX_HEADER_SCAN_ROWS = 10
"""Rows considered when guessing which row holds the header."""

MAX_SAMPLE_ROWS = 5
"""Sample rows returned to the model."""

MAX_COLUMN_SAMPLES = 5
"""Distinct values returned per column."""

DETAILED_PARSE_MAX_BYTES = 10 * 1024 * 1024
"""Above this size the workbook is opened read-only (no merged/formula info)."""

ENUM_MAX_DISTINCT = 12
"""A column with at most this many distinct values may be an enum."""

MIN_ENUM_ROWS = 5
"""Rows needed before a column may be reported as an enum."""

ENUM_MAX_RATIO = 0.6
"""Maximum distinct/row ratio of a categorical column."""

TEXT_AVG_LENGTH = 50
"""Average cell length above which a column is reported as ``text``."""

_TRUE_WORDS = frozenset({"true", "yes", "y", "on"})
_FALSE_WORDS = frozenset({"false", "no", "n", "off"})

_DATE_PATTERNS = (
    re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    re.compile(r"^\d{4}/\d{1,2}/\d{1,2}$"),
    re.compile(r"^\d{4}年\d{1,2}月\d{1,2}日$"),
)
_DATETIME_PATTERNS = (
    re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}"),
    re.compile(r"^\d{4}/\d{1,2}/\d{1,2}[ T]\d{2}:\d{2}"),
    re.compile(r"^\d{4}年\d{1,2}月\d{1,2}日\s+\d{1,2}:\d{2}"),
)

_TOTALS_MARKERS = ("合计", "小计", "总计", "汇总", "总额")
_SNAKE_CASE_RE = re.compile(r"[^a-z0-9]+")
_ASCII_HEADER_RE = re.compile(r"^[A-Za-z0-9 _\-./()]+$")


def _invalid(reason: str) -> OctopError:
    return OctopError(ErrorCode.APP_ROW_INVALID, reason)


@dataclass
class RawSheet:
    """A sheet as raw string cells, before header detection."""

    name: str
    rows: list[list[str]]
    merged: bool = False
    formula_columns: set[int] = field(default_factory=set)


@dataclass(frozen=True)
class ColumnStats:
    """What the model needs to propose a column for one source column."""

    source: str
    name_suggestion: str
    type: str
    non_empty_ratio: float
    distinct: int
    samples: tuple[str, ...]
    max_length: int
    pii: str | None


@dataclass(frozen=True)
class SheetReport:
    """The report of one sheet."""

    name: str
    rows: int
    rows_scanned: int
    header_row: int
    header: tuple[str, ...]
    sample_rows: tuple[tuple[str, ...], ...]
    columns: tuple[ColumnStats, ...]
    warnings: tuple[str, ...]


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _read_delimited(text: str, delimiter: str) -> list[list[str]]:
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    rows: list[list[str]] = []
    for raw in reader:
        rows.append([str(cell).strip() for cell in raw])
    return rows


def _read_json(text: str) -> list[list[str]]:
    payload = json.loads(text)
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise _invalid("JSON inspection requires an array of objects")
    headers: list[str] = []
    for item in payload:
        for key in item:
            name = str(key).strip()
            if name and name not in headers:
                headers.append(name)
    rows = [[str(item.get(key, "") or "").strip() for key in headers] for item in payload]
    return [headers, *rows] if headers else rows


def _read_xlsx(data: bytes, *, detailed: bool) -> list[RawSheet]:
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(data), read_only=not detailed, data_only=True)
    try:
        sheets: list[RawSheet] = []
        for worksheet in workbook.worksheets:
            rows = [
                [_cell_text(value) for value in row]
                for row in worksheet.iter_rows(values_only=True)
            ]
            merged = False
            if detailed:
                merged_ranges = getattr(worksheet, "merged_cells", None)
                merged = bool(getattr(merged_ranges, "ranges", merged_ranges))
            sheets.append(RawSheet(str(worksheet.title), rows, merged))
        return sheets
    finally:
        workbook.close()


def _read_xlsx_formula_columns(data: bytes, limit: int = 20) -> dict[str, set[int]]:
    """Columns that hold formula cells, read with formulas instead of values.

    A second pass is needed because openpyxl returns either the formula or its
    cached value, never both.
    """
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
    try:
        result: dict[str, set[int]] = {}
        for worksheet in workbook.worksheets:
            columns: set[int] = set()
            for index, row in enumerate(worksheet.iter_rows(values_only=True)):
                if index >= limit:
                    break
                for column_index, value in enumerate(row):
                    if isinstance(value, str) and value.startswith("="):
                        columns.add(column_index)
            result[str(worksheet.title)] = columns
        return result
    finally:
        workbook.close()


def _read_xls(data: bytes) -> list[RawSheet]:
    import xlrd

    book = xlrd.open_workbook(file_contents=data)
    sheets: list[RawSheet] = []
    for sheet in book.sheets():
        rows = [
            [
                ""
                if sheet.cell_value(row, col) is None
                else str(sheet.cell_value(row, col)).strip()
                for col in range(sheet.ncols)
            ]
            for row in range(sheet.nrows)
        ]
        sheets.append(RawSheet(str(sheet.name), rows))
    return sheets


def read_sheets(filename: str, data: bytes, *, detailed: bool | None = None) -> list[RawSheet]:
    """Parse *data* into raw sheets, whatever the file type is.

    ``detailed`` adds the merged-cell and formula information a report needs;
    it opens the workbook in full mode, so an importer should pass ``False``
    to keep the memory footprint of a read-only pass.
    """
    if len(data) > MAX_INSPECT_BYTES:
        raise _invalid(
            f"file is too large to inspect (limit {MAX_INSPECT_BYTES // (1024 * 1024)} MB)"
        )
    suffix = PurePosixPath(filename.replace("\\", "/")).suffix.lower()
    if detailed is None:
        detailed = len(data) <= DETAILED_PARSE_MAX_BYTES
    if suffix in (".csv", ".txt"):
        rows = _read_delimited(_decode_text(data), ",")
        return [RawSheet("main", rows)]
    if suffix == ".tsv":
        rows = _read_delimited(_decode_text(data), "\t")
        return [RawSheet("main", rows)]
    if suffix == ".json":
        rows = _read_json(_decode_text(data))
        return [RawSheet("main", rows)]
    if suffix in (".xlsx", ".xlsm"):
        sheets = _read_xlsx(data, detailed=detailed)
        if detailed:
            formulas = _read_xlsx_formula_columns(data)
            for sheet in sheets:
                sheet.formula_columns = formulas.get(sheet.name, set())
        return sheets
    if suffix == ".xls":
        return _read_xls(data)
    raise _invalid(f"unsupported file type: {suffix or '(none)'}")


def _header_score(row: Sequence[str], max_width: int) -> float:
    """How much a row looks like a header row.

    The width matters: a title line ("September payroll") is one cell wide,
    while the real header spans every column, so the wider row wins.
    """
    cells = [cell for cell in row if cell]
    if not cells:
        return 0.0
    filled = len(cells) / max(len(row), 1)
    unique = len(set(cells)) / len(cells)
    textual = sum(1 for cell in cells if not _looks_numeric(cell)) / len(cells)
    coverage = len(row) / max(max_width, 1)
    return filled * 0.4 + unique * 0.25 + textual * 0.15 + coverage * 0.2


def _is_textual(row: Sequence[str]) -> bool:
    """A header row is labels, not numbers."""
    cells = [cell for cell in row if cell]
    if not cells:
        return False
    textual = sum(1 for cell in cells if not _looks_numeric(cell))
    return textual / len(cells) >= 0.5


def _looks_numeric(value: str) -> bool:
    text = value.strip().replace(",", "")
    if not text:
        return False
    try:
        float(text)
    except ValueError:
        return False
    return True


def detect_header_row(rows: Sequence[Sequence[str]]) -> tuple[int, list[str]]:
    """Return the index of the header row and its labels.

    Ledgers often start with a title or a note, so the first row is not
    necessarily the header. The best of the first rows wins; when nothing
    looks like a header the caller is told so with ``-1`` and every row is
    treated as data.
    """
    if not rows:
        return -1, []
    scan = rows[:MAX_HEADER_SCAN_ROWS]
    max_width = max(len(row) for row in scan)
    best_index = -1
    best_score = 0.0
    for index, row in enumerate(scan):
        if not _is_textual(row):
            continue
        score = _header_score(row, max_width)
        if score > best_score:
            best_index = index
            best_score = score
    if best_index < 0:
        return -1, []
    return best_index, _normalize_headers(rows[best_index])


def _normalize_headers(row: Sequence[str]) -> list[str]:
    headers: list[str] = []
    seen: dict[str, int] = {}
    for index, raw in enumerate(row):
        name = str(raw).strip()
        if not name:
            name = f"col_{index + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        headers.append(name)
    return headers


def _is_totals_row(row: Sequence[str]) -> bool:
    first = row[0].strip() if row else ""
    if not any(marker in first for marker in _TOTALS_MARKERS):
        return False
    return all(not cell or _looks_numeric(cell) for cell in row[1:])


def _snake_case(value: str) -> str:
    lowered = value.strip().lower()
    parts = [part for part in _SNAKE_CASE_RE.split(lowered) if part]
    name = "_".join(parts)
    return name if name and not name[0].isdigit() else ""


def suggest_column_name(header: str, index: int) -> str:
    """Propose a snake_case column name for a source header."""
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", header):
        return header
    if _ASCII_HEADER_RE.match(header):
        name = _snake_case(header)
        if name:
            return name
    # Chinese headers become pinyin keys (xing_ming) instead of col_1; the
    # model/user can still rename them while confirming the schema.
    syllables = lazy_pinyin(header, style=Style.NORMAL, errors="ignore")
    name = _snake_case(" ".join(syllables))
    if name:
        return name[:50]
    return f"col_{index + 1}"


def _all_match(values: Sequence[str], predicate: Callable[[str], bool]) -> bool:
    return bool(values) and all(predicate(value) for value in values)


def _is_boolean(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered in _TRUE_WORDS or lowered in _FALSE_WORDS


def _is_integer(value: str) -> bool:
    text = value.strip().replace(",", "")
    if not text:
        return False
    try:
        int(text)
    except ValueError:
        return False
    return True


def _is_number(value: str) -> bool:
    text = value.strip().replace(",", "")
    if not text:
        return False
    try:
        float(text)
    except ValueError:
        return False
    return True


def _is_date(value: str) -> bool:
    text = value.strip()
    if any(pattern.match(text) for pattern in _DATE_PATTERNS):
        return True
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def _is_datetime(value: str) -> bool:
    text = value.strip()
    return any(pattern.match(text) for pattern in _DATETIME_PATTERNS)


def infer_column_type(values: Sequence[str], *, distinct: int, rows: int) -> str:
    """Infer the light-app column type from the sampled values."""
    if not values:
        return "string"
    if _all_match(values, _is_boolean):
        return "boolean"
    if _all_match(values, _is_integer):
        return "integer"
    if _all_match(values, _is_number):
        return "number"
    if _all_match(values, _is_datetime):
        return "datetime"
    if _all_match(values, _is_date):
        return "date"
    non_empty_ratio = len(values) / max(rows, 1)
    # A categorical column repeats its few values; with only a couple of rows
    # every column looks categorical, so require a minimum sample size.
    categorical = (
        rows >= MIN_ENUM_ROWS
        and distinct <= ENUM_MAX_DISTINCT
        and distinct <= max(2, rows * ENUM_MAX_RATIO)
    )
    if categorical and non_empty_ratio > 0.9:
        return "enum"
    if sum(len(value) for value in values) / len(values) > TEXT_AVG_LENGTH:
        return "text"
    return "string"


def detect_pii(header: str, values: Sequence[str]) -> str | None:
    """Return the PII kind of a column, or ``None``.

    Header names win. Value scanning uses the shared free-text detector with a
    majority threshold so one stray number in a note column does not block an
    entire import; public reads still redact individual values.
    """
    header_kind = detect_header_pii(header)
    if header_kind is not None:
        return header_kind
    if not values:
        return None
    counts: dict[str, int] = {}
    for value in values:
        for hit in detect_text_pii(value.strip()):
            counts[hit.kind] = counts.get(hit.kind, 0) + 1
    if not counts:
        return None
    kind, hits = max(counts.items(), key=lambda item: item[1])
    if hits / len(values) >= 0.3:
        return kind
    return None


def inspect_sheet(sheet: RawSheet) -> SheetReport:
    """Build the report of one raw sheet."""
    warnings: list[str] = []
    rows = sheet.rows
    if not rows:
        return SheetReport(sheet.name, 0, 0, -1, (), (), (), tuple(warnings))

    header_index, headers = detect_header_row(rows)
    if header_index < 0:
        warnings.append("no_header_detected")
        data_rows = rows
    else:
        if header_index > 0:
            warnings.append("header_not_first_row")
        data_rows = rows[header_index + 1 :]
    if sheet.merged:
        warnings.append("merged_cells")
    if sheet.formula_columns:
        warnings.append("formula_cells")

    totals = 0
    kept: list[list[str]] = []
    for row in data_rows:
        if _is_totals_row(row):
            totals += 1
            continue
        kept.append(list(row))
    if totals:
        warnings.append("totals_row_detected")

    total_rows = len(kept)
    scanned = kept[:MAX_SCANNED_ROWS]
    if total_rows > MAX_SCANNED_ROWS:
        warnings.append("too_many_rows")

    width = max([len(headers), *(len(row) for row in scanned)])
    headers = list(headers) + [f"col_{index + 1}" for index in range(len(headers), width)]

    columns: list[ColumnStats] = []
    used_names: set[str] = set()
    for column_index, header in enumerate(headers):
        values = [row[column_index] for row in scanned if column_index < len(row)]
        non_empty = [value for value in values if value.strip()]
        distinct_values = list(dict.fromkeys(non_empty))
        distinct = len(distinct_values)
        column_type = infer_column_type(non_empty, distinct=distinct, rows=len(scanned))
        pii_kind = detect_pii(header, non_empty[:200])
        name = suggest_column_name(header, column_index)
        base = name
        suffix = 2
        while name in used_names:
            name = f"{base}_{suffix}"
            suffix += 1
        used_names.add(name)
        columns.append(
            ColumnStats(
                source=header,
                name_suggestion=name,
                type=column_type,
                non_empty_ratio=round(len(non_empty) / max(len(scanned), 1), 3),
                distinct=distinct,
                samples=tuple(distinct_values[:MAX_COLUMN_SAMPLES]),
                max_length=max((len(value) for value in non_empty), default=0),
                pii=pii_kind,
            )
        )

    samples = tuple(
        tuple((row[index] if index < len(row) else "") for index in range(width))
        for row in scanned[:MAX_SAMPLE_ROWS]
    )
    return SheetReport(
        name=sheet.name,
        rows=total_rows,
        rows_scanned=len(scanned),
        header_row=header_index,
        header=tuple(headers),
        sample_rows=samples,
        columns=tuple(columns),
        warnings=tuple(warnings),
    )


def inspect_file(path: str, data: bytes) -> dict[str, Any]:
    """Inspect a spreadsheet file and return a JSON-ready report."""
    filename = path.rsplit("/", 1)[-1] or "upload"
    sheets = read_sheets(filename, data)
    reports = [inspect_sheet(sheet) for sheet in sheets]
    pii_columns = [
        {"sheet": report.name, "column": column.source, "kind": column.pii}
        for report in reports
        for column in report.columns
        if column.pii
    ]
    return {
        "file": path,
        "size_bytes": len(data),
        "sheets": [
            {
                "name": report.name,
                "rows": report.rows,
                "rows_scanned": report.rows_scanned,
                "header_row": report.header_row,
                "header": list(report.header),
                "sample_rows": [list(row) for row in report.sample_rows],
                "columns": [
                    {
                        "source": column.source,
                        "name_suggestion": column.name_suggestion,
                        "type": column.type,
                        "non_empty_ratio": column.non_empty_ratio,
                        "distinct": column.distinct,
                        "samples": list(column.samples),
                        "max_length": column.max_length,
                        "pii": column.pii,
                    }
                    for column in report.columns
                ],
                "warnings": list(report.warnings),
            }
            for report in reports
        ],
        "pii_columns": pii_columns,
    }
