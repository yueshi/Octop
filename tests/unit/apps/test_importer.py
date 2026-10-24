"""Unit tests for light-app import file parsing."""

from __future__ import annotations

import io

import pytest
from openpyxl import Workbook

from octop.infra.apps.importer import decode_text, parse_import_file
from octop.infra.errors import ErrorCode, OctopError


def _xlsx_bytes(rows: list[list[object]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_parse_csv_with_headers() -> None:
    rows = parse_import_file("items.csv", b"name,qty\ncup,3\npen,\n")
    assert rows == [{"name": "cup", "qty": "3"}, {"name": "pen", "qty": ""}]


def test_parse_tsv_and_txt() -> None:
    rows = parse_import_file("items.tsv", b"name\tqty\ncup\t3\n")
    assert rows == [{"name": "cup", "qty": "3"}]
    assert parse_import_file("items.txt", b"a,b\n1,2\n") == [{"a": "1", "b": "2"}]


def test_parse_csv_gb18030_fallback() -> None:
    text = "名称,数量\n杯子,3\n"
    rows = parse_import_file("items.csv", text.encode("gb18030"))
    assert rows == [{"名称": "杯子", "数量": "3"}]
    assert decode_text(text.encode("gb18030")) == text


def test_parse_json_array_of_objects() -> None:
    rows = parse_import_file(
        "items.json", b'[{"name": "cup", "qty": 3}, {"name": "pen", "qty": null}]'
    )
    assert rows == [{"name": "cup", "qty": "3"}, {"name": "pen", "qty": ""}]


def test_parse_json_rejects_non_arrays() -> None:
    with pytest.raises(OctopError) as exc:
        parse_import_file("items.json", b'{"name": "cup"}')
    assert exc.value.code == ErrorCode.APP_ROW_INVALID
    with pytest.raises(OctopError):
        parse_import_file("items.json", b"[1, 2, 3]")


def test_parse_xlsx_first_sheet() -> None:
    data = _xlsx_bytes([["name", "qty"], ["cup", 3], ["pen", None]])
    rows = parse_import_file("items.xlsx", data)
    assert rows == [{"name": "cup", "qty": "3"}, {"name": "pen", "qty": ""}]


def test_unsupported_extension_rejected() -> None:
    with pytest.raises(OctopError) as exc:
        parse_import_file("items.pdf", b"%PDF-1.4")
    assert exc.value.code == ErrorCode.APP_ROW_INVALID
    assert "unsupported" in exc.value.message
