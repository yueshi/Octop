"""Parse external data files into plain rows for light-app import.

CSV / TSV / JSON are stdlib; Excel needs openpyxl / xlrd, which are already
required by the knowledge parser. Text files decode UTF-8 first with GB18030
as fallback — the same policy as ``infra/knowledge/parse.py``.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import PurePosixPath

from octop.infra.errors import ErrorCode, OctopError

TEXT_ENCODINGS = ("utf-8-sig", "gb18030")


def _invalid(reason: str) -> OctopError:
    return OctopError(ErrorCode.APP_ROW_INVALID, reason)


def decode_text(data: bytes) -> str:
    """Decode bytes as UTF-8 (with BOM), falling back to GB18030."""
    for encoding in TEXT_ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _clean_header(name: object) -> str:
    return str(name or "").strip()


def _parse_delimited(text: str, delimiter: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter)
    if not reader.fieldnames:
        return []
    names = [_clean_header(name) for name in reader.fieldnames]
    rows: list[dict[str, str]] = []
    for raw in reader:
        row = {
            name: str(raw.get(field) or "")
            for name, field in zip(names, reader.fieldnames, strict=False)
            if name
        }
        if any(value != "" for value in row.values()):
            rows.append(row)
    return rows


def _parse_json(text: str) -> list[dict[str, str]]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _invalid(f"invalid JSON: {exc.msg}") from None
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise _invalid("JSON import must be an array of objects")
    return [
        {_clean_header(key): str(value if value is not None else "") for key, value in item.items()}
        for item in payload
    ]


def _parse_xlsx(data: bytes) -> list[dict[str, str]]:
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        sheet = workbook.worksheets[0]
        rows_iter = sheet.iter_rows(values_only=True)
        header = next(rows_iter, None)
        if header is None:
            return []
        names = [_clean_header(cell) for cell in header]
        rows: list[dict[str, str]] = []
        for values in rows_iter:
            row = {
                name: "" if value is None else str(value)
                for name, value in zip(names, values, strict=False)
                if name
            }
            if any(value != "" for value in row.values()):
                rows.append(row)
        return rows
    finally:
        workbook.close()


def _parse_xls(data: bytes) -> list[dict[str, str]]:
    import xlrd

    book = xlrd.open_workbook(file_contents=data)
    sheet = book.sheet_by_index(0)
    if sheet.nrows == 0:
        return []
    names = [_clean_header(sheet.cell_value(0, col)) for col in range(sheet.ncols)]
    rows: list[dict[str, str]] = []
    for r in range(1, sheet.nrows):
        row = {
            name: "" if sheet.cell_value(r, col) is None else str(sheet.cell_value(r, col))
            for col, name in enumerate(names)
            if name
        }
        if any(value != "" for value in row.values()):
            rows.append(row)
    return rows


def parse_import_file(filename: str, data: bytes) -> list[dict[str, str]]:
    """Parse an upload or workspace file into rows keyed by source column."""
    suffix = PurePosixPath(filename.replace("\\", "/")).suffix.lower()
    if suffix in (".csv", ".txt"):
        return _parse_delimited(decode_text(data), ",")
    if suffix == ".tsv":
        return _parse_delimited(decode_text(data), "\t")
    if suffix == ".json":
        return _parse_json(decode_text(data))
    if suffix in (".xlsx", ".xlsm"):
        return _parse_xlsx(data)
    if suffix == ".xls":
        return _parse_xls(data)
    raise _invalid(f"unsupported import file type: {suffix or '(none)'}")
