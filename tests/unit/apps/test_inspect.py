"""Unit tests for light-app spreadsheet inspection."""

from __future__ import annotations

import io

import pytest
from openpyxl import Workbook

from octop.infra.apps.inspect import (
    RawSheet,
    inspect_file,
    inspect_sheet,
    suggest_column_name,
)
from octop.infra.errors import ErrorCode, OctopError


def _xlsx_bytes(sheets: dict[str, list[list[object]]]) -> bytes:
    workbook = Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets.items():
        sheet = workbook.create_sheet(title=name)
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _sheet(report: dict, index: int = 0) -> dict:
    return report["sheets"][index]


def _column(report: dict, name: str, index: int = 0) -> dict:
    sheet = _sheet(report, index)
    return next(column for column in sheet["columns"] if column["source"] == name)


def test_inspect_csv_reports_types_and_samples() -> None:
    report = inspect_file(
        "inbound/items.csv",
        b"name,qty,price,done,due\ncup,3,1.5,true,2026-01-02\npen,2,2.5,false,2026-01-03\n",
    )
    sheet = _sheet(report)
    assert sheet["name"] == "main"
    assert sheet["rows"] == 2
    assert sheet["header_row"] == 0
    assert sheet["header"] == ["name", "qty", "price", "done", "due"]
    assert _column(report, "name")["type"] == "string"
    assert _column(report, "qty")["type"] == "integer"
    assert _column(report, "price")["type"] == "number"
    assert _column(report, "done")["type"] == "boolean"
    assert _column(report, "due")["type"] == "date"
    assert _column(report, "qty")["samples"] == ["3", "2"]
    assert sheet["sample_rows"][0] == ["cup", "3", "1.5", "true", "2026-01-02"]


def test_inspect_detects_enum_column() -> None:
    rows = "\n".join(["status", "new", "doing", "done", "new", "doing"]).splitlines()
    report = inspect_file("inbound/tasks.csv", ("\n".join(rows) + "\n").encode())
    column = _column(report, "status")
    assert column["type"] == "enum"
    assert column["distinct"] == 3


def test_inspect_skips_leading_title_row() -> None:
    report = inspect_file(
        "inbound/ledger.csv",
        "2026 年 9 月工资表\n工号,姓名,基本工资\nE001,张悦,8000\nE002,李雷,7000\n".encode(),
    )
    sheet = _sheet(report)
    assert sheet["header_row"] == 1
    assert sheet["header"] == ["工号", "姓名", "基本工资"]
    assert sheet["rows"] == 2
    assert "header_not_first_row" in sheet["warnings"]


def test_inspect_excludes_totals_row() -> None:
    report = inspect_file(
        "inbound/ledger.csv",
        "工号,工资\nE001,8000\nE002,7000\n合计,15000\n".encode(),
    )
    sheet = _sheet(report)
    assert sheet["rows"] == 2
    assert "totals_row_detected" in sheet["warnings"]


def test_inspect_flags_pii_columns() -> None:
    report = inspect_file(
        "inbound/staff.csv",
        "姓名,身份证号,手机号\n张悦,330101199001011234,13800138000\n李雷,330101199002022345,13900139000\n".encode(),
    )
    assert _column(report, "身份证号")["pii"] == "id_card"
    assert _column(report, "手机号")["pii"] == "phone"
    assert _column(report, "姓名")["pii"] is None
    kinds = {entry["column"]: entry["kind"] for entry in report["pii_columns"]}
    assert kinds == {"身份证号": "id_card", "手机号": "phone"}


def test_inspect_reports_too_many_rows_without_truncating_silently() -> None:
    rows = ["id,value"] + [f"{index},{index}" for index in range(20_001)]
    report = inspect_file("inbound/big.csv", ("\n".join(rows) + "\n").encode())
    sheet = _sheet(report)
    assert sheet["rows"] == 20_001
    assert sheet["rows_scanned"] == 20_000
    assert "too_many_rows" in sheet["warnings"]


def test_inspect_xlsx_multiple_sheets() -> None:
    data = _xlsx_bytes(
        {
            "9月": [["工号", "姓名"], ["E001", "张悦"]],
            "10月": [["工号", "姓名"], ["E002", "李雷"]],
        }
    )
    report = inspect_file("inbound/q3.xlsx", data)
    assert [sheet["name"] for sheet in report["sheets"]] == ["9月", "10月"]
    assert _sheet(report, 1)["rows"] == 1
    assert _column(report, "工号", 1)["source"] == "工号"


def test_inspect_xlsx_reports_merged_cells_and_formulas() -> None:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet["A1"] = "工资表"
    sheet.merge_cells("A1:B1")
    sheet.append(["工号", "基本工资"])
    sheet.append(["E001", 8000])
    sheet.append(["合计", "=SUM(B2:B2)"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    report = inspect_file("inbound/pay.xlsx", buffer.getvalue())
    warnings = _sheet(report)["warnings"]
    assert "merged_cells" in warnings
    assert "formula_cells" in warnings


def test_inspect_json_array_of_objects() -> None:
    report = inspect_file(
        "inbound/items.json",
        b'[{"name": "cup", "qty": 3}, {"name": "pen", "qty": 4}]',
    )
    sheet = _sheet(report)
    assert sheet["header"] == ["name", "qty"]
    assert sheet["rows"] == 2


def test_inspect_rejects_unsupported_type() -> None:
    with pytest.raises(OctopError) as exc:
        inspect_file("inbound/notes.pdf", b"%PDF-1.4")
    assert exc.value.code == ErrorCode.APP_ROW_INVALID


def test_inspect_file_without_header_keeps_every_row() -> None:
    report = inspect_sheet(RawSheet("main", [["1", "2"], ["3", "4"]]))
    assert report.header_row == -1
    assert report.rows == 2
    assert "no_header_detected" in report.warnings


def test_suggest_column_name() -> None:
    assert suggest_column_name("base_salary", 0) == "base_salary"
    assert suggest_column_name("Base Salary", 1) == "base_salary"
    assert suggest_column_name("姓名", 2) == "col_3"
