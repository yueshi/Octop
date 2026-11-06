"""Unit tests for the quick-create view renderer and system template registry."""

from __future__ import annotations

import pytest

from octop.infra.apps.models import parse_schema_payload, validate_html
from octop.infra.apps.templates import (
    clean_template_id,
    list_system_templates,
    read_system_template,
)
from octop.infra.apps.viewgen import render_html, suggest_view
from octop.infra.errors import ErrorCode, OctopError

SCHEMA = parse_schema_payload(
    {
        "columns": [
            {"name": "date", "label": "日期", "type": "date"},
            {
                "name": "region",
                "label": "区域",
                "type": "enum",
                "values": ["华东", "华南"],
            },
            {"name": "amount", "label": "金额", "type": "number"},
            {"name": "phone", "label": "电话", "type": "string", "pii": "phone"},
        ]
    }
)

REPORT = {
    "columns": [
        {"name_suggestion": "date", "type": "date", "distinct": 30},
        {"name_suggestion": "region", "type": "enum", "distinct": 2},
        {"name_suggestion": "amount", "type": "number", "distinct": 100},
        {"name_suggestion": "phone", "type": "string", "distinct": 100, "pii": "phone"},
    ]
}


def test_suggest_view_drops_pii_and_builds_kpis_and_charts() -> None:
    spec = suggest_view(REPORT, SCHEMA, view="dashboard", title="销售")
    assert spec["view"] == "dashboard"
    assert [item["column"] for item in spec["kpis"]].count("phone") == 0
    assert spec["charts"]
    assert "phone" not in spec["table"]["columns"]


def test_ledger_and_board_skip_charts() -> None:
    ledger = suggest_view(REPORT, SCHEMA, view="ledger")
    board = suggest_view(REPORT, SCHEMA, view="board")
    assert ledger["charts"] == []
    assert board["charts"] == []
    assert ledger["view"] == "ledger"
    assert board["group_by"] == "region"


@pytest.mark.parametrize("view", ["dashboard", "ledger", "board"])
def test_render_html_is_self_contained_and_valid(view: str) -> None:
    spec = suggest_view(REPORT, SCHEMA, view=view, title="销售")
    html = render_html(SCHEMA, spec, title="销售", description="测试")
    validate_html(html)
    assert "octopApp.listRows" in html
    assert "http://" not in html
    assert "https://" not in html


def test_system_templates_are_shipped_and_valid() -> None:
    templates = {item.template_id: item for item in list_system_templates("zh")}
    assert {"customer-followup", "expense-tracker"} <= set(templates)
    assert templates["customer-followup"].name == "客户跟进台账"
    assert templates["customer-followup"].scope == "global"

    schema_text, html_text = read_system_template("customer-followup")
    parse_schema_payload(schema_text)
    assert html_text and "octopApp" in html_text


def test_template_id_rejects_traversal() -> None:
    with pytest.raises(OctopError) as exc:
        clean_template_id("../secret")
    assert exc.value.code == ErrorCode.APP_NOT_FOUND
