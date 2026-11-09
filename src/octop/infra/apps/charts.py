"""Turn app rows into a small declarative chart spec.

The app assistant asks the model for a chart *request* (type, x, y, agg),
never for the numbers themselves. This module validates that request against
the app schema, aggregates the real rows, and returns the data the dashboard
renders with Recharts. Keeping the aggregation server-side prevents the model
from inventing series values and keeps PII columns out of charts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from octop.i18n import tr
from octop.infra.apps.models import Schema
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.utils.locale import Locale

CHART_TYPES = frozenset({"bar", "line", "pie", "area", "scatter", "stacked_bar"})
CHART_AGGS = frozenset({"sum", "avg", "count", "min", "max"})
NUMERIC_TYPES = frozenset({"number", "integer"})
MAX_SERIES = 3
MAX_CHART_LIMIT = 50
MAX_SCATTER_POINTS = 200
DEFAULT_CHART_LIMIT = 20
DEFAULT_SCATTER_LIMIT = 100
MAX_TITLE_LEN = 120


def _invalid(message: str, **details: object) -> OctopError:
    return OctopError(
        ErrorCode.APP_SCHEMA_INVALID,
        message,
        details=details,
    )


def _cell_text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _to_float(value: object) -> float | None:
    text = _cell_text(value).replace(",", "")
    for symbol in ("¥", "$", "€", "£", "%"):
        text = text.replace(symbol, "")
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _sort_key(value: str) -> tuple[int, float | str]:
    try:
        return (0, float(value.replace(",", "")))
    except ValueError:
        return (1, value)


def _aggregate(values: Sequence[float], agg: str) -> float:
    if agg == "sum":
        return float(sum(values))
    if agg == "avg":
        return float(sum(values) / len(values))
    if agg == "min":
        return float(min(values))
    if agg == "max":
        return float(max(values))
    return float(len(values))


def build_chart_spec(
    schema: Schema,
    rows: Sequence[Mapping[str, Any]],
    *,
    chart_type: str,
    x: str,
    y: Sequence[str] | None,
    agg: str,
    title: str | None,
    limit: int,
    locale: Locale = "zh",
) -> dict[str, Any]:
    """Validate a chart request and aggregate *rows* into a renderable spec."""
    kind = str(chart_type or "bar").strip().lower()
    if kind not in CHART_TYPES:
        raise _invalid(f"unsupported chart type: {chart_type!r}")
    aggregation = str(agg or "sum").strip().lower()
    # Scatter plots use raw row points; aggregation is not applicable.
    if kind != "scatter" and aggregation not in CHART_AGGS:
        raise _invalid(f"unsupported aggregation: {agg!r}")
    if kind == "scatter":
        aggregation = "sum"

    x_name = str(x or "").strip()
    x_column = schema.column(x_name)
    if x_column is None:
        raise _invalid(f"unknown x column: {x_name!r}")
    if x_column.pii:
        raise _invalid(f"x column {x_name!r} contains sensitive data")
    if kind == "scatter" and x_column.type not in NUMERIC_TYPES:
        raise _invalid(f"scatter x column {x_name!r} is not numeric ({x_column.type})")

    y_names: list[str] = []
    if aggregation != "count":
        for name in y or ():
            clean = str(name).strip()
            if clean and clean not in y_names:
                y_names.append(clean)
        if not y_names:
            raise _invalid("y must name at least one numeric column")
        if len(y_names) > MAX_SERIES:
            raise _invalid(f"at most {MAX_SERIES} y columns are supported")
    if kind == "pie" and len(y_names) > 1:
        raise _invalid("pie charts support exactly one y column")
    if kind == "scatter" and len(y_names) != 1:
        raise _invalid("scatter charts support exactly one y column")

    series_labels: list[str] = []
    for name in y_names:
        column = schema.column(name)
        if column is None:
            raise _invalid(f"unknown y column: {name!r}")
        if column.pii:
            raise _invalid(f"y column {name!r} contains sensitive data")
        if column.type not in NUMERIC_TYPES:
            raise _invalid(f"y column {name!r} is not numeric ({column.type})")
        if name == x_name:
            raise _invalid("x and y must be different columns")
        series_labels.append(column.label)

    if kind == "scatter":
        safe_limit = max(
            1,
            min(int(limit or DEFAULT_SCATTER_LIMIT), MAX_SCATTER_POINTS),
        )
        points: list[dict[str, Any]] = []
        for row in rows:
            x_value = _to_float(row.get(x_name))
            y_value = _to_float(row.get(y_names[0]))
            if x_value is None or y_value is None:
                continue
            points.append({"x": x_value, y_names[0]: y_value})
        points.sort(key=lambda item: item["x"])
        truncated = len(points) > safe_limit
        points = points[:safe_limit]
        chart_title = (
            title.strip()[:MAX_TITLE_LEN]
            if title and title.strip()
            else tr(
                "light_apps.assistant_chart_title_scatter",
                locale,
                x=x_column.label,
                y=series_labels[0],
            )
        )
        return {
            "type": kind,
            "title": chart_title,
            "x": x_name,
            "x_label": x_column.label,
            "series": [{"key": y_names[0], "label": series_labels[0]}],
            "data": points,
            "truncated": truncated,
        }

    safe_limit = max(1, min(int(limit or DEFAULT_CHART_LIMIT), MAX_CHART_LIMIT))
    totals: dict[str, int] = {}
    buckets: dict[str, dict[str, list[float]]] = {}
    for row in rows:
        key = _cell_text(row.get(x_name))
        if not key:
            continue
        totals[key] = totals.get(key, 0) + 1
        bucket = buckets.setdefault(key, {})
        for name in y_names:
            value = _to_float(row.get(name))
            if value is not None:
                bucket.setdefault(name, []).append(value)

    if aggregation == "count":
        series = [
            {
                "key": "count",
                "label": tr("light_apps.assistant_chart_count", locale),
            }
        ]
    else:
        series = [
            {"key": name, "label": label}
            for name, label in zip(y_names, series_labels, strict=True)
        ]

    data: list[dict[str, Any]] = []
    for key, bucket in buckets.items():
        if aggregation == "count":
            point: dict[str, Any] = {"x": key, "count": float(totals[key])}
        else:
            point = {"x": key}
            missing = False
            for name in y_names:
                values = bucket.get(name) or []
                if not values and kind == "pie":
                    missing = True
                    break
                point[name] = _aggregate(values, aggregation) if values else 0.0
            if missing or (kind == "pie" and not point.get(y_names[0])):
                continue
        data.append(point)

    if kind in {"line", "area"}:
        data.sort(key=lambda item: _sort_key(str(item["x"])))
    elif kind == "stacked_bar":
        data.sort(
            key=lambda item: (
                -sum(float(item.get(entry["key"]) or 0) for entry in series),
                _sort_key(str(item["x"])),
            )
        )
    else:
        first_key = series[0]["key"]
        data.sort(
            key=lambda item: (
                -float(item.get(first_key) or 0),
                _sort_key(str(item["x"])),
            )
        )

    truncated = False
    if len(data) > safe_limit:
        truncated = True
        if kind in {"line", "area"}:
            step = len(data) / safe_limit
            data = [data[int(index * step)] for index in range(safe_limit)]
        else:
            data = data[:safe_limit]

    if title and title.strip():
        chart_title = title.strip()[:MAX_TITLE_LEN]
    elif aggregation == "count":
        chart_title = tr(
            "light_apps.assistant_chart_title_count",
            locale,
            group=x_column.label,
        )
    elif kind in {"line", "area"}:
        chart_title = tr(
            "light_apps.assistant_chart_title_trend",
            locale,
            metric=" / ".join(series_labels),
        )
    else:
        chart_title = tr(
            "light_apps.assistant_chart_title_group",
            locale,
            metric=" / ".join(series_labels),
            group=x_column.label,
        )

    return {
        "type": kind,
        "title": chart_title,
        "x": x_name,
        "x_label": x_column.label,
        "series": series,
        "data": data,
        "truncated": truncated,
    }


__all__ = [
    "CHART_AGGS",
    "CHART_TYPES",
    "build_chart_spec",
]
