"""Parse external data files into plain rows for light-app import.

CSV / TSV / JSON are stdlib; Excel needs openpyxl / xlrd, which are already
required by the knowledge parser. Text files decode UTF-8 first with GB18030
as fallback — the same policy as ``infra/knowledge/parse.py``.

The file parsing itself lives in :mod:`octop.infra.apps.inspect`, so a report
and an import always agree on what a sheet contains.
"""

from __future__ import annotations

from octop.infra.apps.inspect import RawSheet, read_sheets
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


def _select_sheet(sheets: list[RawSheet], sheet: str | None) -> RawSheet:
    """Pick a worksheet by name or 0-based index; the first one by default."""
    if not sheets:
        raise _invalid("file has no sheets")
    if sheet is None or not sheet.strip():
        return sheets[0]
    wanted = sheet.strip()
    if wanted.lstrip("-").isdigit():
        index = int(wanted)
        if 0 <= index < len(sheets):
            return sheets[index]
        raise _invalid(f"sheet index out of range: {wanted}")
    for candidate in sheets:
        if candidate.name.strip().lower() == wanted.lower():
            return candidate
    raise _invalid(f"sheet not found: {wanted}")


def _header_pairs(row: list[str]) -> list[tuple[int, str]]:
    """Column positions and names of a header row (blank names are skipped)."""
    pairs: list[tuple[int, str]] = []
    for index, cell in enumerate(row):
        name = str(cell).strip()
        if name:
            pairs.append((index, name))
    return pairs


def parse_import_file(
    filename: str,
    data: bytes,
    *,
    sheet: str | None = None,
    header_row: int | None = None,
) -> list[dict[str, str]]:
    """Parse an upload or workspace file into rows keyed by source column.

    ``sheet`` selects a worksheet by name or 0-based index in Excel files.
    ``header_row`` is the 0-based index of the header line, so a ledger that
    starts with a title can be imported from its real header.
    """
    sheets = read_sheets(filename, data, detailed=False)
    raw = _select_sheet(sheets, sheet)
    rows = raw.rows
    head = 0 if header_row is None else max(0, header_row)
    if head >= len(rows):
        return []
    pairs = _header_pairs(rows[head])
    records: list[dict[str, str]] = []
    for values in rows[head + 1 :]:
        record = {name: (values[index] if index < len(values) else "") for index, name in pairs}
        if any(value != "" for value in record.values()):
            records.append(record)
    return records
