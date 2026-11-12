"""Read-only public surface for light apps.

A publication token is the credential: no JWT, no cookies. The API returns the
app shell and its rows; the host page embeds the shell in the same sandboxed
iframe the dashboard uses, with a bridge that only supports reads.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from octop.api.common.client_ip import resolve_client_ip
from octop.api.deps import get_server
from octop.api.routers.apps import _columns, _service
from octop.infra.apps.limits import public_slot
from octop.infra.apps.models import MAIN_TABLE_KEY
from octop.infra.errors import ErrorCode, OctopError

router = APIRouter(prefix="/public/apps")
page_router = APIRouter()

_NO_STORE = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


class PublicAppOut(BaseModel):
    app_id: str
    name: str
    description: str
    html: str
    tables: list[dict[str, Any]]


class PublicRowsOut(BaseModel):
    columns: list[dict[str, Any]]
    rows: list[dict[str, str]]
    total: int
    limit: int
    offset: int
    table: str
    pii_columns: list[str]


def _audit_public(server: Any, app_id: str, action: str) -> None:
    if getattr(server, "services", None) is None:
        return
    server.services.audit_repo.write(actor="_public", action=action, target=app_id)


def _public_key(token: str, request: Request) -> str:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"{digest}:{resolve_client_ip(request)}"


async def _public_limit(token: str, request: Request) -> AsyncIterator[None]:
    async with public_slot(_public_key(token, request)):
        yield


def _public_app(server: Any, token: str) -> tuple[Any, Any]:
    service = _service(server)
    row = service.resolve_public_app(token)
    if row is None:
        raise OctopError(ErrorCode.APP_NOT_FOUND, "public app not found")
    return service, row


async def _public_payload(service: Any, row: Any) -> tuple[str, list[dict[str, Any]]]:
    """The frozen published version, or the live app for a legacy publication."""
    snapshot = service.public_snapshot(row)
    if snapshot is not None:
        frozen_html, frozen_tables = snapshot
        return str(frozen_html), list(frozen_tables)
    tables: list[dict[str, Any]] = []
    for table in service.list_tables(row):
        if not table.public_visible:
            continue
        schema = await service.read_schema(row, table.table_key)
        tables.append(
            {
                "table_key": table.table_key,
                "label": table.label,
                "row_count": table.row_count,
                "is_main": table.is_main,
                "columns": [column.model_dump() for column in _columns(schema)],
            }
        )
    return await service.read_html(row) or "", tables


@router.get("/{token}", response_model=PublicAppOut, summary="Get a public app")
async def get_public_app(
    token: str,
    _limit: None = Depends(_public_limit),
    server: Any = Depends(get_server),
) -> JSONResponse:
    from octop.infra.metrics import METRICS  # noqa: PLC0415

    METRICS.inc("light_app_public_view_total")
    service, row = _public_app(server, token)
    _audit_public(server, row.app_id, "light_app.public.view")
    html, tables = await _public_payload(service, row)
    payload = PublicAppOut(
        app_id=row.app_id,
        name=row.name,
        description=row.description,
        html=html,
        tables=tables,
    )
    return JSONResponse(payload.model_dump(), headers=_NO_STORE)


@router.get(
    "/{token}/rows",
    response_model=PublicRowsOut,
    summary="Read a public app table",
)
async def get_public_rows(
    token: str,
    table: str = Query(default=MAIN_TABLE_KEY),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _limit: None = Depends(_public_limit),
    server: Any = Depends(get_server),
) -> JSONResponse:
    from octop.infra.metrics import METRICS  # noqa: PLC0415

    METRICS.inc("light_app_public_view_total")
    service, row = _public_app(server, token)
    snapshot = service.public_snapshot(row)
    snapshot_table: dict[str, Any] | None = None
    if snapshot is not None:
        _html, tables = snapshot
        snapshot_table = next(
            (item for item in tables if item.get("table_key") == table),
            None,
        )
        # The published surface is the snapshot: a table added after publish
        # is not visible until the owner republishes.
        if snapshot_table is None:
            raise OctopError(ErrorCode.APP_NOT_FOUND, "public app table not found")
    elif not service.is_table_public(row, table):
        raise OctopError(ErrorCode.APP_NOT_FOUND, "public app table not found")

    schema, rows, _meta, total = await service.read_rows_detailed(
        row,
        limit=limit,
        offset=offset,
        table=table,
        redact_pii=True,
    )
    if snapshot_table is not None:
        snapshot_columns = snapshot_table.get("columns")
        if not isinstance(snapshot_columns, list):
            snapshot_columns = []
        live_pii = {column.name: column.pii for column in schema.columns}
        columns = [
            {**column, "pii": live_pii.get(column.get("name"), column.get("pii"))}
            for column in snapshot_columns
            if isinstance(column, dict) and isinstance(column.get("name"), str)
        ]
        allowed = {column["name"] for column in columns}
        rows = [{key: value for key, value in row.items() if key in allowed} for row in rows]
    else:
        columns = [column.model_dump() for column in _columns(schema)]
    payload = PublicRowsOut(
        columns=columns,
        rows=rows,
        total=total,
        limit=limit,
        offset=offset,
        table=table,
        pii_columns=[column["name"] for column in columns if column.get("pii")],
    )
    return JSONResponse(payload.model_dump(), headers=_NO_STORE)


_SDK = """
(function () {
  var pending = new Map();
  var seq = 0;
  function call(method, params) {
    return new Promise(function (resolve, reject) {
      var id = ++seq;
      pending.set(id, { resolve: resolve, reject: reject });
      parent.postMessage(
        { source: "octop-app", type: "request", id: id, method: method, params: params || {} },
        "*"
      );
    });
  }
  window.addEventListener("message", function (event) {
    var data = event.data;
    if (!data || data.source !== "octop-app-host") return;
    var entry = pending.get(data.id);
    if (!entry) return;
    pending.delete(data.id);
    if (data.ok) entry.resolve(data.data);
    else entry.reject(new Error(data.error || "Request failed"));
  });
  window.octopApp = {
    schema: function (table) { return call("schema", { table: table || null }); },
    tables: function () { return call("tables"); },
    listRows: function (options) { return call("rows", options || {}); },
    me: function () { return Promise.resolve({ permission: "view" }); },
    appendRow: function () {
      return Promise.reject(new Error("This is a read-only public link"));
    },
    generate: function () {
      return Promise.reject(new Error("This is a read-only public link"));
    },
    launchGame: function () {
      return Promise.reject(new Error("This is a read-only public link"));
    }
  };
})();
"""

_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data: blob:; font-src data:; media-src data: blob:; "
    "connect-src 'none'; base-uri 'none'; form-action 'none'"
)


def _host_page(token: str, app: PublicAppOut) -> str:
    # Escape "</" so a script tag inside the app cannot close the host script.
    app_html = json.dumps(app.html).replace("</", "<\\/")
    token_json = json.dumps(token)
    sdk = json.dumps(_SDK).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_escape(app.name)}</title>
<style>html,body{{margin:0;height:100%;background:#fff}}iframe{{border:0;width:100%;height:100%}}</style>
</head><body>
<iframe id="app" sandbox="allow-scripts"></iframe>
<script>
const appHtml = {app_html};
const token = {token_json};
const sdk = {sdk};
const csp = {json.dumps(_CSP)};
const head = '<meta http-equiv="Content-Security-Policy" content="' + csp + '"><script>' + sdk + '<\\/script>';
const frame = document.getElementById("app");
function withHead(html) {{
  const m = /<head[^>]*>/i.exec(html);
  if (m) return html.slice(0, m.index + m[0].length) + head + html.slice(m.index + m[0].length);
  return "<!doctype html><html><head>" + head + "</head><body>" + html + "</body></html>";
}}
frame.srcdoc = withHead(appHtml);
window.addEventListener("message", async function (event) {{
  const data = event.data;
  if (!data || data.source !== "octop-app" || data.type !== "request" || typeof data.id !== "number") return;
  const reply = (payload) => frame.contentWindow.postMessage(Object.assign({{source: "octop-app-host", id: data.id}}, payload), "*");
  try {{
    if (data.method === "tables") {{
      const res = await fetch("/api/public/apps/" + encodeURIComponent(token), {{cache: "no-store"}});
      if (!res.ok) throw new Error("HTTP " + res.status);
      const app = await res.json();
      reply({{ok: true, data: {{tables: app.tables.map((t) => ({{table_key: t.table_key, label: t.label, row_count: t.row_count, is_main: t.is_main}}))}}}});
    }} else if (data.method === "schema" || data.method === "rows") {{
      const table = data.params?.table || "main";
      const qs = new URLSearchParams({{table: table, limit: String(data.params?.limit || 200), offset: String(data.params?.offset || 0)}});
      const res = await fetch("/api/public/apps/" + encodeURIComponent(token) + "/rows?" + qs.toString(), {{cache: "no-store"}});
      if (!res.ok) throw new Error("HTTP " + res.status);
      const body = await res.json();
      if (data.method === "schema") reply({{ok: true, data: {{table: table, columns: body.columns}}}});
      else reply({{ok: true, data: body}});
    }} else {{
      reply({{ok: false, error: "unsupported method"}});
    }}
  }} catch (err) {{
    reply({{ok: false, error: err instanceof Error ? err.message : String(err)}});
  }}
}});
</script></body></html>"""


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


@page_router.get("/p/{token}", response_class=HTMLResponse, include_in_schema=False)
async def public_app_page(
    token: str,
    _limit: None = Depends(_public_limit),
    server: Any = Depends(get_server),
) -> HTMLResponse:
    from octop.infra.metrics import METRICS  # noqa: PLC0415

    METRICS.inc("light_app_public_view_total")
    service, row = _public_app(server, token)
    html, tables = await _public_payload(service, row)
    app = PublicAppOut(
        app_id=row.app_id,
        name=row.name,
        description=row.description,
        html=html,
        tables=tables,
    )
    return HTMLResponse(_host_page(token, app), headers=_NO_STORE)
