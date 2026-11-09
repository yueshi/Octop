"""Unit tests for the app assistant's declarative chart builder."""

from __future__ import annotations

import pytest

from octop.infra.apps.charts import build_chart_spec
from octop.infra.apps.models import Column, Schema
from octop.infra.errors import ErrorCode, OctopError

SCHEMA = Schema(
    columns=(
        Column(name="status", label="状态", type="enum", values=("new", "done")),
        Column(name="qty", label="数量", type="integer"),
        Column(name="amount", label="金额", type="number"),
        Column(name="day", label="日期", type="date"),
        Column(name="email", label="邮箱", type="string", pii="email"),
    )
)

ROWS = [
    {"status": "new", "qty": "2", "amount": "10", "day": "2026-01-03", "email": "a@x"},
    {"status": "new", "qty": "3", "amount": "5", "day": "2026-01-01", "email": "b@x"},
    {"status": "done", "qty": "1", "amount": "7", "day": "2026-01-02", "email": "c@x"},
]


def _build(**overrides: object) -> dict[str, object]:
    params: dict[str, object] = {
        "chart_type": "bar",
        "x": "status",
        "y": ["qty"],
        "agg": "sum",
        "title": None,
        "limit": 20,
        "locale": "zh",
    }
    params.update(overrides)
    return build_chart_spec(SCHEMA, ROWS, **params)  # type: ignore[arg-type]


def test_bar_sums_and_sorts_by_value() -> None:
    spec = _build()
    assert spec["type"] == "bar"
    assert spec["title"] == "数量（按状态）"
    assert spec["series"] == [{"key": "qty", "label": "数量"}]
    assert spec["data"] == [
        {"x": "new", "qty": 5.0},
        {"x": "done", "qty": 1.0},
    ]


def test_line_sorts_by_x() -> None:
    spec = _build(chart_type="line", x="day", y=["qty"])
    assert [point["x"] for point in spec["data"]] == [
        "2026-01-01",
        "2026-01-02",
        "2026-01-03",
    ]
    assert spec["title"] == "数量趋势"


def test_count_ignores_y() -> None:
    spec = _build(y=None, agg="count")
    assert spec["series"] == [{"key": "count", "label": "记录数"}]
    assert spec["data"] == [
        {"x": "new", "count": 2.0},
        {"x": "done", "count": 1.0},
    ]
    assert spec["title"] == "状态（记录数）"


def test_avg_aggregation() -> None:
    spec = _build(y=["amount"], agg="avg")
    assert spec["data"] == [
        {"x": "new", "amount": 7.5},
        {"x": "done", "amount": 7.0},
    ]


def test_limit_truncates_bar_series() -> None:
    spec = _build(limit=1)
    assert spec["data"] == [{"x": "new", "qty": 5.0}]
    assert spec["truncated"] is True


def test_scatter_uses_raw_numeric_points() -> None:
    spec = _build(
        chart_type="scatter",
        x="qty",
        y=["amount"],
        agg="none",
    )
    assert spec["type"] == "scatter"
    assert spec["series"] == [{"key": "amount", "label": "金额"}]
    assert spec["data"] == [
        {"x": 1.0, "amount": 7.0},
        {"x": 2.0, "amount": 10.0},
        {"x": 3.0, "amount": 5.0},
    ]
    assert spec["title"] == "金额 与 数量 的关系"


def test_scatter_rejects_non_numeric_x_and_multiple_y() -> None:
    with pytest.raises(OctopError):
        _build(chart_type="scatter", x="status", y=["amount"])
    with pytest.raises(OctopError):
        _build(chart_type="scatter", x="qty", y=["amount", "qty"])


def test_scatter_limit_truncates_points() -> None:
    spec = _build(chart_type="scatter", x="qty", y=["amount"], limit=2)
    assert len(spec["data"]) == 2
    assert spec["truncated"] is True


def test_stacked_bar_sums_multiple_series_by_total() -> None:
    spec = _build(
        chart_type="stacked_bar",
        x="status",
        y=["qty", "amount"],
        agg="sum",
    )
    assert spec["type"] == "stacked_bar"
    assert spec["series"] == [
        {"key": "qty", "label": "数量"},
        {"key": "amount", "label": "金额"},
    ]
    assert spec["data"] == [
        {"x": "new", "qty": 5.0, "amount": 15.0},
        {"x": "done", "qty": 1.0, "amount": 7.0},
    ]
    assert spec["title"] == "数量 / 金额（按状态）"


def test_pie_rejects_multiple_series() -> None:
    with pytest.raises(OctopError) as exc:
        _build(chart_type="pie", y=["qty", "amount"])
    assert exc.value.code == ErrorCode.APP_SCHEMA_INVALID


def test_pii_columns_are_rejected() -> None:
    for kwargs in ({"x": "email"}, {"y": ["email"]}):
        with pytest.raises(OctopError) as exc:
            _build(**kwargs)
        assert exc.value.code == ErrorCode.APP_SCHEMA_INVALID


def test_unknown_and_non_numeric_columns_are_rejected() -> None:
    with pytest.raises(OctopError):
        _build(x="nope")
    with pytest.raises(OctopError):
        _build(y=["status"])


def test_english_titles() -> None:
    spec = _build(locale="en")
    assert spec["title"] == "数量 by 状态"
