"""Deterministic HTML rendering for the quick-create path.

The quick mode must feel instant: parse a ledger, infer a schema, then produce
a useful dashboard without waiting for an LLM. This module turns the inspection
report into a small view spec and renders a self-contained page that reads rows
through the injected ``octopApp`` SDK.
"""

from __future__ import annotations

import html
import json
from typing import Any

from octop.infra.apps.models import Schema

VIEW_DASHBOARD = "dashboard"
VIEW_LEDGER = "ledger"
VIEW_BOARD = "board"
VIEWS = (VIEW_DASHBOARD, VIEW_LEDGER, VIEW_BOARD)

MAX_TABLE_COLUMNS = 8
MAX_FILTERS = 3
CATEGORICAL_DISTINCT = 12


def suggest_view(
    report: dict[str, Any],
    schema: Schema,
    *,
    view: str = VIEW_DASHBOARD,
    title: str = "",
) -> dict[str, Any]:
    """Build a stable view spec from an inspection report and schema."""
    if view not in VIEWS:
        view = VIEW_DASHBOARD
    columns = [column for column in schema.columns if not column.pii]
    stats = {
        str(item.get("name_suggestion") or ""): item
        for item in report.get("columns", [])
        if isinstance(item, dict)
    }
    numeric = [column for column in columns if column.type in {"number", "integer"}]
    dates = [column for column in columns if column.type in {"date", "datetime"}]
    categorical = [column for column in columns if _is_categorical(column, stats)]

    kpis: list[dict[str, Any]] = [
        {"column": "", "agg": "count", "label": "记录数"},
    ]
    for column in numeric[:2]:
        kpis.append({"column": column.name, "agg": "sum", "label": f"{column.label}合计"})
    if numeric:
        kpis.append({"column": numeric[0].name, "agg": "avg", "label": f"{numeric[0].label}均值"})

    charts: list[dict[str, Any]] = []
    if dates and numeric:
        charts.append(
            {
                "type": "line",
                "x": dates[0].name,
                "y": numeric[0].name,
                "agg": "sum",
                "label": f"{numeric[0].label}趋势",
            }
        )
    if categorical:
        target = numeric[0] if numeric else None
        charts.append(
            {
                "type": "bar",
                "x": categorical[0].name,
                "y": target.name if target else "",
                "agg": "sum" if target else "count",
                "label": f"按{categorical[0].label}分布",
            }
        )
    if len(categorical) > 1:
        target = numeric[0] if numeric else None
        charts.append(
            {
                "type": "bar",
                "x": categorical[1].name,
                "y": target.name if target else "",
                "agg": "sum" if target else "count",
                "label": f"按{categorical[1].label}分布",
            }
        )

    filters = [column.name for column in (*categorical, *dates)][:MAX_FILTERS]
    table_columns = [column.name for column in columns][:MAX_TABLE_COLUMNS]
    return {
        "view": view,
        "title": title,
        "kpis": kpis,
        "charts": charts if view == VIEW_DASHBOARD else [],
        "filters": filters,
        "group_by": categorical[0].name if categorical else "",
        "table": {"columns": table_columns, "page_size": 50},
    }


def render_html(
    schema: Schema,
    spec: dict[str, Any],
    *,
    title: str,
    description: str = "",
) -> str:
    """Render one self-contained dashboard/ledger/board page."""
    config = {
        "title": title,
        "description": description,
        "spec": spec,
        "columns": [
            {
                "name": column.name,
                "label": column.label,
                "type": column.type,
                "pii": column.pii,
                "values": list(column.values),
            }
            for column in schema.columns
        ],
    }
    payload = json.dumps(config, ensure_ascii=False).replace("</", "<\\/")
    return _TEMPLATE.replace("__TITLE__", html.escape(title or "小程序")).replace(
        "__CONFIG__", payload
    )


def _is_categorical(column: Any, stats: dict[str, Any]) -> bool:
    if column.type in {"enum", "boolean"}:
        return True
    if column.type != "string":
        return False
    info = stats.get(column.name) or {}
    try:
        distinct = int(info.get("distinct") or 0)
    except (TypeError, ValueError):
        return False
    return 0 < distinct <= CATEGORICAL_DISTINCT


_TEMPLATE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root { --bg:#f6f7f9; --card:#fff; --line:#e5e7eb; --text:#1f2937; --muted:#6b7280; --brand:#2563eb; }
  * { box-sizing:border-box; }
  body { margin:0; padding:18px; background:var(--bg); color:var(--text); font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif; font-size:14px; }
  .page { max-width:1180px; margin:0 auto; }
  header { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; }
  h1 { margin:0; font-size:22px; }
  .sub { margin:6px 0 0; color:var(--muted); font-size:13px; }
  .toolbar { display:flex; flex-wrap:wrap; gap:8px; margin:16px 0 12px; }
  select, input { font:inherit; color:inherit; background:var(--card); border:1px solid var(--line); border-radius:8px; padding:7px 10px; outline:none; }
  select:focus, input:focus { border-color:var(--brand); }
  .search { flex:1 1 220px; }
  .kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-bottom:14px; }
  .kpi { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }
  .kpi .label { color:var(--muted); font-size:12px; }
  .kpi strong { display:block; margin-top:6px; font-size:24px; font-variant-numeric:tabular-nums; }
  .charts { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:12px; margin-bottom:14px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }
  .card h2 { margin:0 0 12px; font-size:13px; color:var(--muted); font-weight:600; }
  .bars { display:flex; flex-direction:column; gap:9px; }
  .bar-row { display:grid; grid-template-columns:minmax(60px,120px) minmax(0,1fr) 84px; gap:8px; align-items:center; }
  .bar-name { color:var(--muted); font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .bar-track { height:9px; border-radius:999px; background:#f1f5f9; overflow:hidden; }
  .bar-fill { height:100%; border-radius:999px; background:var(--brand); }
  .bar-value { text-align:right; font-size:12px; font-variant-numeric:tabular-nums; }
  .table-wrap { background:var(--card); border:1px solid var(--line); border-radius:10px; overflow-x:auto; }
  table { width:100%; border-collapse:collapse; min-width:640px; }
  th, td { padding:9px 12px; text-align:left; border-bottom:1px solid var(--line); vertical-align:top; }
  th { background:#fafafa; color:var(--muted); font-size:12px; font-weight:600; white-space:nowrap; }
  tbody tr:last-child td { border-bottom:0; }
  tbody tr:hover { background:#fafcff; }
  td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
  .pager { display:flex; justify-content:flex-end; align-items:center; gap:8px; margin-top:10px; color:var(--muted); font-size:12px; }
  .pager button { font:inherit; border:1px solid var(--line); background:var(--card); border-radius:7px; padding:5px 10px; cursor:pointer; }
  .pager button:disabled { opacity:.45; cursor:default; }
  .board { display:flex; gap:12px; overflow-x:auto; padding-bottom:6px; }
  .board-col { flex:0 0 240px; background:var(--card); border:1px solid var(--line); border-radius:10px; padding:10px; }
  .board-col h3 { margin:0 0 8px; font-size:13px; display:flex; justify-content:space-between; }
  .board-col h3 span { color:var(--muted); font-weight:400; }
  .board-card { border:1px solid var(--line); border-radius:8px; padding:8px 10px; margin-bottom:8px; background:#fafafa; }
  .board-card b { display:block; font-size:13px; margin-bottom:3px; }
  .board-card small { color:var(--muted); }
  .empty { text-align:center; color:var(--muted); padding:36px 0; }
  .hidden { display:none !important; }
  @media (max-width:700px) { body { padding:12px; } }
</style>
</head>
<body>
<div class="page">
  <header>
    <div><h1 id="title"></h1><p class="sub" id="description"></p></div>
  </header>
  <section class="toolbar" id="toolbar">
    <input class="search" id="search" placeholder="搜索…">
  </section>
  <section class="kpis" id="kpis"></section>
  <section class="charts" id="charts"></section>
  <section class="board hidden" id="board"></section>
  <section class="table-wrap" id="table-wrap">
    <table><thead id="thead"></thead><tbody id="tbody"></tbody></table>
    <div class="pager"><span id="page-info"></span><button id="prev" type="button">上一页</button><button id="next" type="button">下一页</button></div>
  </section>
  <p class="empty hidden" id="empty">还没有数据。</p>
</div>
<script>
(function () {
  "use strict";
  var CONFIG = __CONFIG__;
  var SPEC = CONFIG.spec || {};
  var COLUMNS = CONFIG.columns || [];
  var COLUMN_MAP = {};
  for (var i = 0; i < COLUMNS.length; i++) COLUMN_MAP[COLUMNS[i].name] = COLUMNS[i];
  var rows = [];
  var filtered = [];
  var page = 0;
  var filters = {};
  var query = "";

  function $(selector) { return document.querySelector(selector); }
  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function number(value) { var parsed = Number(value); return isFinite(parsed) ? parsed : 0; }
  function fmt(value) {
    var parsed = number(value);
    return parsed.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
  }
  function label(name) { return (COLUMN_MAP[name] && COLUMN_MAP[name].label) || name; }
  function typeOf(name) { return (COLUMN_MAP[name] && COLUMN_MAP[name].type) || ""; }
  function isNumeric(name) { var type = typeOf(name); return type === "number" || type === "integer"; }
  function cellHtml(name, value) {
    if (value == null || value === "") return '<span style="color:var(--muted)">—</span>';
    if (isNumeric(name)) return '<span class="num">' + escapeHtml(fmt(value)) + "</span>";
    return escapeHtml(value);
  }
  function distinct(name) {
    var seen = {}, values = [];
    for (var i = 0; i < rows.length; i++) {
      var value = rows[i][name];
      if (value == null || value === "" || seen[value]) continue;
      seen[value] = true;
      values.push(value);
    }
    values.sort();
    return values;
  }
  function aggregate(name, op) {
    if (op === "count" || !name) return filtered.length;
    var total = 0;
    for (var i = 0; i < filtered.length; i++) total += number(filtered[i][name]);
    return op === "avg" ? (filtered.length ? total / filtered.length : 0) : total;
  }
  function applyFilters() {
    var q = query.trim().toLowerCase();
    filtered = rows.filter(function (row) {
      for (var name in filters) {
        if (filters[name] && String(row[name] == null ? "" : row[name]) !== filters[name]) return false;
      }
      if (!q) return true;
      for (var key in row) {
        if (String(row[key] == null ? "" : row[key]).toLowerCase().indexOf(q) >= 0) return true;
      }
      return false;
    });
    page = 0;
    render();
  }
  function renderKpis() {
    if (SPEC.view === "ledger") { $("#kpis").classList.add("hidden"); return; }
    $("#kpis").classList.remove("hidden");
    $("#kpis").innerHTML = (SPEC.kpis || []).map(function (kpi) {
      var value = kpi.agg === "count" ? aggregate("", "count") : aggregate(kpi.column, kpi.agg);
      var text = kpi.agg === "count" ? fmt(value) : (isNumeric(kpi.column) ? fmt(value) : value);
      return '<div class="kpi"><div class="label">' + escapeHtml(kpi.label || label(kpi.column)) + '</div><strong>' + escapeHtml(text) + "</strong></div>";
    }).join("");
  }
  function groupSums(x, y, op) {
    var map = {};
    for (var i = 0; i < filtered.length; i++) {
      var key = String(filtered[i][x] == null ? "" : filtered[i][x]) || "（空）";
      map[key] = (map[key] || 0) + (op === "count" ? 1 : number(filtered[i][y]));
    }
    return Object.keys(map).map(function (key) { return { key: key, value: map[key] }; })
      .sort(function (a, b) { return b.value - a.value; }).slice(0, 12);
  }
  function renderBars(chart) {
    var entries = groupSums(chart.x, chart.y, chart.agg);
    var max = entries.length ? entries[0].value : 0;
    var bars = entries.map(function (entry) {
      var width = max ? Math.max(2, Math.round(entry.value / max * 100)) : 0;
      return '<div class="bar-row"><span class="bar-name" title="' + escapeHtml(entry.key) + '">' + escapeHtml(entry.key) +
        '</span><div class="bar-track"><div class="bar-fill" style="width:' + width + '%"></div></div><span class="bar-value">' +
        escapeHtml(fmt(entry.value)) + "</span></div>";
    }).join("");
    return '<div class="card"><h2>' + escapeHtml(chart.label || "分布") + '</h2><div class="bars">' + (bars || '<p class="empty">暂无数据</p>') + "</div></div>";
  }
  function renderLine(chart) {
    var buckets = groupSums(chart.x, chart.y, chart.agg).sort(function (a, b) { return a.key < b.key ? -1 : 1; });
    if (buckets.length < 2) return renderBars(chart);
    var width = 560, height = 170, pad = 24;
    var max = 0;
    buckets.forEach(function (entry) { if (entry.value > max) max = entry.value; });
    max = max || 1;
    var points = buckets.map(function (entry, index) {
      var x = pad + (index / (buckets.length - 1)) * (width - pad * 2);
      var y = height - pad - (entry.value / max) * (height - pad * 2);
      return x.toFixed(1) + "," + y.toFixed(1);
    }).join(" ");
    return '<div class="card"><h2>' + escapeHtml(chart.label || "趋势") + '</h2><svg viewBox="0 0 ' + width + " " + height +
      '" style="width:100%;height:190px" role="img"><polyline fill="none" stroke="#2563eb" stroke-width="2" points="' + points +
      '"></polyline></svg></div>';
  }
  function renderCharts() {
    if (SPEC.view !== "dashboard") { $("#charts").classList.add("hidden"); return; }
    $("#charts").classList.remove("hidden");
    $("#charts").innerHTML = (SPEC.charts || []).map(function (chart) {
      return chart.type === "line" ? renderLine(chart) : renderBars(chart);
    }).join("");
  }
  function renderTable() {
    if (SPEC.view === "board") { $("#table-wrap").classList.add("hidden"); return; }
    $("#table-wrap").classList.remove("hidden");
    var columns = (SPEC.table && SPEC.table.columns) || COLUMNS.map(function (c) { return c.name; });
    $("#thead").innerHTML = "<tr>" + columns.map(function (name) {
      return '<th class="' + (isNumeric(name) ? "num" : "") + '">' + escapeHtml(label(name)) + "</th>";
    }).join("") + "</tr>";
    var size = (SPEC.table && SPEC.table.page_size) || 50;
    var start = page * size;
    var slice = filtered.slice(start, start + size);
    $("#tbody").innerHTML = slice.map(function (row) {
      return "<tr>" + columns.map(function (name) { return "<td>" + cellHtml(name, row[name]) + "</td>"; }).join("") + "</tr>";
    }).join("");
    var pages = Math.max(1, Math.ceil(filtered.length / size));
    $("#page-info").textContent = "第 " + (page + 1) + " / " + pages + " 页，共 " + filtered.length + " 条";
    $("#prev").disabled = page <= 0;
    $("#next").disabled = page >= pages - 1;
    $("#empty").classList.toggle("hidden", filtered.length > 0);
  }
  function renderBoard() {
    if (SPEC.view !== "board") { $("#board").classList.add("hidden"); return; }
    $("#board").classList.remove("hidden");
    $("#table-wrap").classList.add("hidden");
    var group = SPEC.group_by;
    if (!group) { $("#board").innerHTML = '<p class="empty">没有可用于分组的字段。</p>'; return; }
    var groups = {};
    filtered.forEach(function (row) {
      var key = String(row[group] == null ? "" : row[group]) || "（空）";
      (groups[key] = groups[key] || []).push(row);
    });
    var columns = (SPEC.table && SPEC.table.columns) || [];
    $("#board").innerHTML = Object.keys(groups).sort().map(function (key) {
      var cards = groups[key].slice(0, 50).map(function (row) {
        var title = columns[0] ? row[columns[0]] : "";
        var meta = columns.slice(1, 3).filter(function (name) { return row[name]; }).map(function (name) {
          return escapeHtml(label(name)) + "：" + escapeHtml(row[name]);
        }).join(" · ");
        return '<div class="board-card"><b>' + escapeHtml(title || "未命名") + "</b><small>" + meta + "</small></div>";
      }).join("");
      return '<div class="board-col"><h3>' + escapeHtml(key) + "<span>" + groups[key].length + "</span></h3>" + cards + "</div>";
    }).join("");
  }
  function renderFilters() {
    var names = SPEC.filters || [];
    var html = '<input class="search" id="search" placeholder="搜索…">';
    names.forEach(function (name) {
      var values = distinct(name);
      html += '<select data-filter="' + escapeHtml(name) + '"><option value="">全部' + escapeHtml(label(name)) + "</option>" +
        values.map(function (value) { return '<option value="' + escapeHtml(value) + '">' + escapeHtml(value) + "</option>"; }).join("") +
        "</select>";
    });
    $("#toolbar").innerHTML = html;
    var search = $("#search");
    search.value = query;
    search.addEventListener("input", function () { query = search.value; applyFilters(); });
    document.querySelectorAll("[data-filter]").forEach(function (select) {
      select.value = filters[select.getAttribute("data-filter")] || "";
      select.addEventListener("change", function () {
        filters[select.getAttribute("data-filter")] = select.value;
        applyFilters();
      });
    });
  }
  function render() {
    renderKpis();
    renderCharts();
    renderTable();
    renderBoard();
  }
  async function loadRows() {
    var all = [], offset = 0, total = null;
    while (total === null || all.length < total) {
      var result = await octopApp.listRows({ limit: 500, offset: offset });
      var batch = (result && result.rows) || [];
      all = all.concat(batch);
      total = result && typeof result.total === "number" ? result.total : all.length;
      if (!batch.length) break;
      offset += batch.length;
      if (all.length >= 20000) break;
    }
    return all;
  }
  async function boot() {
    $("#title").textContent = CONFIG.title || "小程序";
    $("#description").textContent = CONFIG.description || "";
    document.title = CONFIG.title || "小程序";
    try {
      rows = await loadRows();
      filtered = rows.slice();
      renderFilters();
      render();
    } catch (error) {
      $("#empty").classList.remove("hidden");
      $("#empty").textContent = "加载失败：" + (error && error.message ? error.message : "未知错误");
    }
  }
  $("#prev").addEventListener("click", function () { if (page > 0) { page -= 1; renderTable(); } });
  $("#next").addEventListener("click", function () { page += 1; renderTable(); });
  boot();
})();
</script>
</body>
</html>
"""
