"""Built-in LangChain tools for agent-managed MiniApps."""

from __future__ import annotations

import json
from typing import Annotated, Any

import httpx
from langchain_core.tools import StructuredTool
from langgraph.config import get_config
from pydantic import Field

from octop.infra.agents.tool_scope import current_tool_scope
from octop.infra.apps import capabilities as app_capabilities
from octop.infra.apps.models import MAIN_TABLE_KEY
from octop.infra.apps.pii import detect_text_pii, find_unmarked_pii
from octop.infra.apps.service import (
    FILE_PREVIEW,
    MAX_ASSET_BYTES,
    AppService,
    AppTableView,
    app_path,
)
from octop.infra.db.repos.apps import PERMISSION_RANK, LightAppRow

APP_DELETE_TOOL_NAME = "app_delete"

APP_TOOL_NAMES: tuple[str, ...] = (
    "app_inspect_file",
    "app_list",
    "app_open",
    "app_share_link",
    "app_create",
    "app_create_from_template",
    "app_add_table",
    "app_update_schema",
    "app_alter_schema",
    "app_set_table_visibility",
    "app_set_capabilities",
    "app_write_html",
    "app_read_data",
    "app_append_row",
    "app_update_row",
    "app_delete_row",
    "app_import_rows",
    "app_save_asset",
    "app_set_preview",
    APP_DELETE_TOOL_NAME,
)
"""Names of the built-in light-app tools, in the order they are registered."""

_COLUMNS_HELP = (
    "Column definitions. Each item: "
    '{"name": "snake_case identifier", "label": "display label", '
    '"type": "string|text|integer|number|boolean|date|datetime|enum", '
    '"required": false, "values": ["only for enum"], '
    '"pii": "required for sensitive data: id_card|bank_card|phone|address|salary|email|secret"}. '
    "Up to 50 columns; names must start with a letter or underscore. "
    "Never put a phone, ID, bank card, email or address in a free-text/note "
    "column: mark the column pii instead."
)

_TABLE_HELP = (
    "Which table to use. Every app has a main table named 'main'; "
    "tables created with app_add_table use their own key. "
    "Omit it to read or write the main table."
)

_HTML_HELP = (
    "Complete single-file HTML with inline CSS/JS. It renders inside a sandboxed "
    "iframe (scripts allowed, no same-origin access and no network fetch). "
    "Inside the page use the injected `window.octopApp` SDK: "
    "`await octopApp.schema(table)` returns the column definitions; "
    "`await octopApp.listRows({table, limit, offset})` returns {rows, total}; "
    "`await octopApp.tables()` returns the tables visible to the current viewer; "
    "render tabs from that response instead of hardcoding table keys, because "
    "public links omit private tables. "
    "`await octopApp.appendRow({column: value}, {table})` adds a row when the "
    "viewer has edit permission. Remote scripts/styles/images/fonts are blocked, "
    "so keep CSS/JS inline and use data: or blob: URLs for images; relative URLs "
    "and fetch() to the platform API are not allowed. Call octopApp.listRows() on "
    "load to render current data."
)


def _tool_ctx() -> tuple[str, int]:
    scope = current_tool_scope()
    if scope is not None:
        return scope
    cfg = get_config().get("configurable") or {}
    agent_id = cfg.get("agent_id")
    user_raw = cfg.get("user")
    if not agent_id:
        raise ValueError("missing configurable.agent_id")
    if user_raw is None:
        raise ValueError("missing configurable.user")
    return str(agent_id), int(user_raw)


def _tool_source() -> str:
    """Channel source for the current turn, e.g. ``weixin/01M5...``."""
    cfg = get_config().get("configurable") or {}
    return str(cfg.get("octop_source") or "").strip()


def _is_dashboard_source(source: str) -> bool:
    return not source or source.split("/", 1)[0] in {"dashboard", "web"}


def _ok(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _err(exc: Exception) -> str:
    return json.dumps({"error": str(exc)}, ensure_ascii=False)


def _get_for_actor(
    service: AppService,
    app_id: str,
    agent_id: str,
    user_id: int,
    needed: str,
) -> LightAppRow:
    """Resolve an app for the acting user, using the app ACL.

    The agent is only the executor: what the user may do with an app comes
    from the app's own collaborator list, so a shared agent's members can use
    the apps they were given and nobody inherits access from owning an agent.
    """
    if not service.is_agent_accessible(agent_id, user_id):
        raise ValueError("agent is not accessible to this user")
    row = service.get(app_id)
    if row is None:
        raise ValueError(f"MiniApp not found: {app_id}")
    granted = service.permission_for(row, user_id, is_admin=False)
    if granted is None or PERMISSION_RANK.get(granted, 0) < PERMISSION_RANK[needed]:
        raise ValueError(
            f"this user has no '{needed}' permission on MiniApp {app_id} "
            f"(granted: {granted or 'none'})"
        )
    return row


def _may_see_pii(service: AppService, row: LightAppRow, user_id: int) -> bool:
    """Editors and owners see sensitive columns; viewers do not."""
    permission = service.permission_for(row, user_id, is_admin=False)
    return PERMISSION_RANK.get(permission or "", 0) >= PERMISSION_RANK["edit_data"]


def _summary(row: LightAppRow) -> dict[str, Any]:
    return {
        "app_id": row.app_id,
        "name": row.name,
        "description": row.description,
        "row_count": row.row_count,
        "capabilities": app_capabilities.describe(row.capabilities_json),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


async def _app_card(service: AppService, row: LightAppRow) -> dict[str, Any]:
    """Structured chat card for an app (open/created app)."""
    return {
        "octop_ui": {"renderer": "app_card", "version": 1},
        "data": {
            **_summary(row),
            "agent_id": row.agent_id,
            "has_html": await service.has_html(row),
            "has_preview": await service.has_preview(row),
        },
        "text": f"MiniApp {row.name} ({row.app_id})",
    }


def _table_summary(table: AppTableView) -> dict[str, Any]:
    return {
        "table_key": table.table_key,
        "label": table.label,
        "row_count": table.row_count,
        "is_main": table.is_main,
    }


async def _read_asset_source(service: AppService, agent_id: str, source: str) -> bytes:
    """Read the bytes of an image from a URL or from the agent workspace."""
    text = source.strip()
    if text.startswith(("http://", "https://")):
        return await _download_asset(text)
    return await service.read_workspace_file(agent_id, text)


async def _download_asset(url: str) -> bytes:
    """Download an image, refusing private and loopback targets on every hop.

    The body is capped while it streams: a server that lies about its size
    must not be able to fill memory.
    """
    from octop_harness.security.ssrf import validate_agent_fetch_url

    current = validate_agent_fetch_url(url)
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
        for _ in range(5):
            async with client.stream("GET", current) as response:
                if response.is_redirect:
                    location = (response.headers.get("location") or "").strip()
                    if not location:
                        break
                    current = validate_agent_fetch_url(str(response.url.join(location)))
                    continue
                if response.is_error:
                    raise ValueError(f"download failed: HTTP {response.status_code}")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_ASSET_BYTES:
                        raise ValueError("asset exceeds the 5 MB limit")
                    chunks.append(chunk)
                return b"".join(chunks)
    raise ValueError("too many redirects while downloading the asset")


async def _share_link_warning(service: AppService, row: LightAppRow) -> str | None:
    """Describe what a public link actually exposes, for the model to relay."""
    tables = service.list_tables(row)
    public = [table for table in tables if table.public_visible]
    private = [table for table in tables if not table.public_visible]
    pii_columns: list[str] = []
    unmarked: list[str] = []
    for table in public:
        schema = await service.read_schema(row, table.table_key)
        pii_columns.extend(
            f"{table.table_key}.{column.name}" for column in schema.columns if column.pii
        )
        _schema, rows, _total = await service.read_rows(
            row,
            limit=50,
            table=table.table_key,
            redact_pii=False,
        )
        seen: set[str] = set()
        for values in rows:
            for column, _hit in find_unmarked_pii(
                ((item.name, item.pii, item.type) for item in schema.columns),
                values,
            ):
                key = f"{table.table_key}.{column}"
                if key not in seen:
                    seen.add(key)
                    unmarked.append(key)
    if not private and not pii_columns and not unmarked:
        return None
    lines = ["Sharing notice for the public link:"]
    lines.append("Public tables: " + (", ".join(table.table_key for table in public) or "(none)"))
    if private:
        lines.append(
            "Private tables (not in the public link): "
            + ", ".join(table.table_key for table in private)
        )
    if pii_columns:
        lines.append("Sensitive columns masked on public reads: " + ", ".join(pii_columns))
    if unmarked:
        lines.append(
            "Unmarked PII detected in public tables: "
            + ", ".join(unmarked)
            + ". Mark these columns as pii or keep the table private before sharing."
        )
    html = await service.read_html(row)
    if html and detect_text_pii(html):
        lines.append("PII detected in the app HTML itself; remove it from the page before sharing.")
    return "\n".join(lines)


async def _share_link_payload(
    service: AppService,
    row: LightAppRow,
    *,
    user_id: int,
    rotate: bool,
    ttl_days: int,
    public_base_url: str,
) -> list[dict[str, Any]]:
    """Channel-ready blocks: one plain-text link plus an optional cover."""
    token, _expires_at = await service.publish(
        row,
        user_id=user_id,
        rotate=rotate,
        ttl_days=ttl_days,
    )
    path = f"/p/{token}"
    url = f"{public_base_url.rstrip('/')}{path}" if public_base_url else path
    lines = [row.name]
    if row.description:
        lines.append(row.description)
    lines.append(url)
    blocks: list[dict[str, Any]] = [{"type": "text", "text": "\n".join(lines)}]
    if await service.has_preview(row):
        preview = app_path(row.app_id, FILE_PREVIEW)
        blocks.append(
            {
                "type": "image",
                "path": preview,
                "source": {"type": "url", "url": preview, "media_type": "image/svg+xml"},
                "filename": FILE_PREVIEW,
                # iLink image messages need a raster format; dashboard can
                # still render the SVG through its preview endpoint.
                "im_safe": False,
            }
        )
    return blocks


def build_app_tools(
    service: AppService,
    *,
    public_base_url: str = "",
) -> list[StructuredTool]:
    """Return built-in light-app tools (wired via ``HarnessAgentConfig.tools``)."""

    async def app_inspect_file(
        path: Annotated[
            str,
            Field(
                description=(
                    "Workspace-relative path of the file to inspect "
                    "(CSV/TSV/JSON/.xlsx/.xls), e.g. inbound/imports/sales.xlsx. "
                    "Hidden files (starting with a dot) are rejected."
                )
            ),
        ],
    ) -> str:
        try:
            agent_id, user_id = _tool_ctx()
            if not service.is_agent_accessible(agent_id, user_id):
                raise ValueError("agent is not accessible to this user")
            report = await service.inspect_workspace_file(agent_id, path)
            return _ok(
                {
                    "octop_ui": {"renderer": "app_inspect_card", "version": 1},
                    "data": report,
                    "text": (
                        f"Inspected {report['file']}: "
                        f"{len(report['sheets'])} sheet(s), "
                        f"{len(report['pii_columns'])} sensitive column(s)."
                    ),
                }
            )
        except Exception as exc:
            return _err(exc)

    async def app_list() -> str:
        try:
            agent_id, user_id = _tool_ctx()
            if not service.is_agent_accessible(agent_id, user_id):
                return _ok({"apps": []})
            rows = service.list_for_agent(agent_id, user_id)
            return _ok(
                {
                    "apps": [
                        {
                            **_summary(row),
                            "tables": [_table_summary(table) for table in service.list_tables(row)],
                        }
                        for row in rows
                    ]
                }
            )
        except Exception as exc:
            return _err(exc)

    async def app_open(
        app_id: Annotated[
            str,
            Field(description="App id returned by app_list/app_create."),
        ],
    ) -> str:
        try:
            agent_id, user_id = _tool_ctx()
            row = _get_for_actor(service, app_id, agent_id, user_id, "view")
            source = _tool_source()
            if _is_dashboard_source(source):
                return _ok(await _app_card(service, row))
            # IM channels cannot render the dashboard app_card. A public link
            # plus a plain-text description is the channel-safe equivalent.
            try:
                _get_for_actor(service, app_id, agent_id, user_id, "edit_structure")
                payload = await _share_link_payload(
                    service,
                    row,
                    user_id=user_id,
                    rotate=False,
                    ttl_days=0,
                    public_base_url=public_base_url,
                )
                warning = await _share_link_warning(service, row)
                if warning:
                    payload.append({"type": "text", "text": warning})
                return _ok(payload)
            except ValueError:
                lines = [row.name]
                if row.description:
                    lines.append(row.description)
                return _ok([{"type": "text", "text": "\n".join(lines)}])
        except Exception as exc:
            return _err(exc)

    async def app_share_link(
        app_id: Annotated[
            str | None,
            Field(description="App id; omit when passing name."),
        ] = None,
        name: Annotated[
            str | None,
            Field(description="App name (exact or substring match); omit when passing app_id."),
        ] = None,
        rotate: Annotated[
            bool,
            Field(
                description=(
                    "Rotate the public link. Leave false to reuse the current link; "
                    "rotating invalidates links already sent to users."
                )
            ),
        ] = False,
        ttl_days: Annotated[
            int,
            Field(ge=0, le=3650, description="Link lifetime in days; 0 means no expiry."),
        ] = 0,
    ) -> str:
        try:
            agent_id, user_id = _tool_ctx()
            if app_id:
                row = _get_for_actor(service, app_id, agent_id, user_id, "edit_structure")
            elif name and name.strip():
                needle = name.strip().lower()
                rows = service.list_for_agent(agent_id, user_id)
                exact = [
                    candidate for candidate in rows if candidate.name.strip().lower() == needle
                ]
                matches = exact or [
                    candidate for candidate in rows if needle in candidate.name.lower()
                ]
                if not matches:
                    raise ValueError(f"MiniApp not found: {name}")
                if len(matches) > 1:
                    options = ", ".join(f"{item.name} ({item.app_id})" for item in matches[:5])
                    raise ValueError(f"more than one MiniApp matches {name!r}: {options}")
                row = matches[0]
                service.require(row, user_id, "edit_structure", is_admin=False)
            else:
                raise ValueError("app_id or name is required")
            payload = await _share_link_payload(
                service,
                row,
                user_id=user_id,
                rotate=rotate,
                ttl_days=ttl_days,
                public_base_url=public_base_url,
            )
            warning = await _share_link_warning(service, row)
            if warning:
                payload.append({"type": "text", "text": warning})
            return _ok(payload)
        except Exception as exc:
            return _err(exc)

    async def app_create(
        name: Annotated[
            str,
            Field(description="Short display name shown in the app list (max 100 characters)."),
        ],
        columns: Annotated[
            list[dict[str, Any]],
            Field(description=_COLUMNS_HELP),
        ],
        description: Annotated[
            str,
            Field(description="Optional one-line description shown in the app list."),
        ] = "",
        capabilities: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Page capabilities to declare. Omit for the safe default: data.read, "
                    "data.write, asset.read, external.open, chat.compose, events.read. "
                    "High-risk actions (ai.generate, game.launch, external.fetch, "
                    "events.emit, schedules.manage) must be declared explicitly."
                )
            ),
        ] = None,
    ) -> str:
        try:
            agent_id, user_id = _tool_ctx()
            if not service.is_agent_accessible(agent_id, user_id):
                raise ValueError("this user cannot use this agent")
            row = await service.create(
                agent_id=agent_id,
                owner_user_id=user_id,
                name=name,
                description=description,
                columns=columns,
                capabilities=capabilities,
            )
            return _ok({**_summary(row), "next_step": "call app_write_html to build the UI"})
        except Exception as exc:
            return _err(exc)

    async def app_update_schema(
        app_id: Annotated[str, Field(description="App id returned by app_create/app_list.")],
        columns: Annotated[
            list[dict[str, Any]],
            Field(
                description=(
                    "Columns to append. Existing columns cannot be renamed or removed. "
                    "New columns cannot be required when the app already has rows. " + _COLUMNS_HELP
                )
            ),
        ],
        table: Annotated[
            str,
            Field(description=_TABLE_HELP),
        ] = MAIN_TABLE_KEY,
    ) -> str:
        try:
            agent_id, user_id = _tool_ctx()
            row = _get_for_actor(service, app_id, agent_id, user_id, "edit_structure")
            schema = await service.add_columns(row, columns, table=table)
            return _ok(
                {
                    "app_id": row.app_id,
                    "table": table,
                    "version": schema.version,
                    "columns": schema.to_dict()["columns"],
                }
            )
        except Exception as exc:
            return _err(exc)

    async def app_alter_schema(
        app_id: Annotated[str, Field(description="App id returned by app_create/app_list.")],
        renames: Annotated[
            dict[str, str] | None,
            Field(description="Old column name -> new column name."),
        ] = None,
        drops: Annotated[
            list[str] | None,
            Field(description="Columns to remove; their data is deleted."),
        ] = None,
        types: Annotated[
            dict[str, dict[str, Any]] | None,
            Field(
                description=(
                    "Column name -> {type, label?, required?, values?}. Types: "
                    "string, text, integer, number, boolean, date, datetime, enum."
                )
            ),
        ] = None,
        pii: Annotated[
            dict[str, str | None] | None,
            Field(
                description=(
                    "Column name -> sensitive-data kind (id_card, bank_card, phone, "
                    "address, salary, email, secret); pass null to clear. Use this "
                    "for existing sensitive columns before writing free-text PII."
                )
            ),
        ] = None,
        table: Annotated[str, Field(description=_TABLE_HELP)] = MAIN_TABLE_KEY,
    ) -> str:
        try:
            agent_id, user_id = _tool_ctx()
            app = _get_for_actor(service, app_id, agent_id, user_id, "edit_structure")
            schema = await service.alter_schema(
                app,
                table=table,
                renames=renames,
                drops=drops,
                types=types,
                pii=pii,
            )
            return _ok(
                {
                    "app_id": app.app_id,
                    "table": table,
                    "version": schema.version,
                    "columns": schema.to_dict()["columns"],
                }
            )
        except Exception as exc:
            return _err(exc)

    async def app_set_table_visibility(
        app_id: Annotated[str, Field(description="App id returned by app_create/app_list.")],
        public: Annotated[
            bool,
            Field(
                description=(
                    "True to expose this table on the public read-only link; false "
                    "to keep it visible only to signed-in users with access."
                )
            ),
        ],
        table: Annotated[str, Field(description=_TABLE_HELP)] = MAIN_TABLE_KEY,
    ) -> str:
        try:
            agent_id, user_id = _tool_ctx()
            row = _get_for_actor(service, app_id, agent_id, user_id, "edit_structure")
            view = await service.set_table_visibility(
                row,
                table=table,
                public_visible=public,
            )
            return _ok(
                {
                    "app_id": row.app_id,
                    "table": view.table_key,
                    "public_visible": view.public_visible,
                }
            )
        except Exception as exc:
            return _err(exc)

    async def app_set_capabilities(
        app_id: Annotated[str, Field(description="App id returned by app_create/app_list.")],
        capabilities: Annotated[
            list[str],
            Field(
                description=(
                    "Complete page capability list. Pass [] to deny every page action; "
                    "omit high-risk actions the app does not need (ai.generate, "
                    "game.launch, external.fetch, events.emit, schedules.manage)."
                )
            ),
        ],
    ) -> str:
        try:
            agent_id, user_id = _tool_ctx()
            row = _get_for_actor(service, app_id, agent_id, user_id, "edit_structure")
            row = service.set_capabilities(row, capabilities)
            return _ok(_summary(row))
        except Exception as exc:
            return _err(exc)

    async def app_write_html(
        app_id: Annotated[str, Field(description="App id returned by app_create/app_list.")],
        html: Annotated[str, Field(description=_HTML_HELP)],
    ) -> str:
        try:
            agent_id, user_id = _tool_ctx()
            row = _get_for_actor(service, app_id, agent_id, user_id, "edit_structure")
            size = await service.write_html(row, html)
            # Writing the shell is an internal edit, not a "show the app" action.
            # Only app_open/app_create_from_template return a chat card; emitting
            # one here would duplicate the card whenever the agent opens the app.
            return _ok(
                {
                    **_summary(row),
                    "bytes": size,
                    "has_html": True,
                    "next_step": (
                        "After the first complete usable version, call app_open once "
                        "to show the entry; do not call app_open after later edits."
                    ),
                }
            )
        except Exception as exc:
            return _err(exc)

    async def app_create_from_template(
        template: Annotated[
            str,
            Field(description="Template id from the agent workspace or the system catalog."),
        ],
        source: Annotated[
            str,
            Field(description="'agent' (workspace template, default) or 'system' (built-in)."),
        ] = "agent",
        name: Annotated[
            str | None,
            Field(description="Optional app name; defaults to the template name."),
        ] = None,
        description: Annotated[str, Field(description="Optional app description.")] = "",
    ) -> str:
        try:
            agent_id, user_id = _tool_ctx()
            if not service.is_agent_accessible(agent_id, user_id):
                raise ValueError("agent is not accessible to this user")
            row = await service.create_from_template(
                agent_id=agent_id,
                owner_user_id=user_id,
                template=template,
                source=source,
                name=name,
                description=description,
            )
            card = await _app_card(service, row)
            card["data"]["next_step"] = "call app_write_html only if you want to change the shell"
            return _ok({**card["data"], **card})
        except Exception as exc:
            return _err(exc)

    async def app_add_table(
        app_id: Annotated[str, Field(description="App id returned by app_create/app_list.")],
        key: Annotated[
            str,
            Field(
                description=(
                    "Machine key for the new table, e.g. 'orders'. Use letters, digits "
                    "and underscores; 'main' is taken."
                )
            ),
        ],
        columns: Annotated[
            list[dict[str, Any]],
            Field(description="Column definitions for the new table. " + _COLUMNS_HELP),
        ],
        label: Annotated[
            str,
            Field(description="Display name for the table (optional)."),
        ] = "",
        public: Annotated[
            bool,
            Field(
                description=(
                    "Expose this table on the public read-only link. Defaults to "
                    "false; only the main table is public by default."
                )
            ),
        ] = False,
    ) -> str:
        try:
            agent_id, user_id = _tool_ctx()
            row = _get_for_actor(service, app_id, agent_id, user_id, "edit_structure")
            table = await service.create_table(
                row,
                table_key=key,
                label=label,
                columns=columns,
                public_visible=public,
            )
            return _ok(
                {
                    "app_id": row.app_id,
                    "table": table.table_key,
                    "label": table.label,
                    "public_visible": table.public_visible,
                    "columns": columns,
                }
            )
        except Exception as exc:
            return _err(exc)

    async def app_read_data(
        app_id: Annotated[str, Field(description="App id returned by app_create/app_list.")],
        limit: Annotated[int, Field(description="Maximum rows to return (1-500).")] = 100,
        offset: Annotated[int, Field(description="Rows to skip, for paging.")] = 0,
        table: Annotated[
            str,
            Field(description=_TABLE_HELP),
        ] = MAIN_TABLE_KEY,
    ) -> str:
        try:
            agent_id, user_id = _tool_ctx()
            row = _get_for_actor(service, app_id, agent_id, user_id, "view")
            redact_pii = not _may_see_pii(service, row, user_id)
            schema, rows, row_meta, total = await service.read_rows_detailed(
                row,
                limit=limit,
                offset=offset,
                table=table,
                redact_pii=redact_pii,
            )
            pii_columns = [column.name for column in schema.columns if column.pii]
            return _ok(
                {
                    "app_id": row.app_id,
                    "table": table,
                    "columns": schema.to_dict()["columns"],
                    "rows": rows,
                    "row_meta": row_meta,
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                    "pii_columns": pii_columns,
                    "pii_masked": redact_pii,
                    "pii_note": (
                        "Tool output may be masked by the platform for safety; "
                        "masking does not mean the stored value is masked or erased."
                    ),
                }
            )
        except Exception as exc:
            return _err(exc)

    async def app_append_row(
        app_id: Annotated[str, Field(description="App id returned by app_create/app_list.")],
        row: Annotated[
            dict[str, Any],
            Field(
                description=(
                    "One row keyed by column name. Missing optional columns become empty; "
                    "unknown columns and type mismatches are rejected."
                )
            ),
        ],
        table: Annotated[
            str,
            Field(description=_TABLE_HELP),
        ] = MAIN_TABLE_KEY,
        client_request_id: Annotated[
            str | None,
            Field(
                description=(
                    "Optional idempotency key. Retrying with the same value does "
                    "not create a duplicate row."
                )
            ),
        ] = None,
    ) -> str:
        try:
            agent_id, user_id = _tool_ctx()
            app = _get_for_actor(service, app_id, agent_id, user_id, "edit_data")
            index, stored = await service.append_row_record(
                app,
                row,
                table=table,
                client_request_id=client_request_id,
                created_by=user_id,
                source="agent",
            )
            schema = await service.read_schema(app, table)
            return _ok(
                {
                    "app_id": app.app_id,
                    "table": table,
                    "row_index": index,
                    "row_id": stored.row_id,
                    "version": stored.version,
                    "pii_columns": [column.name for column in schema.columns if column.pii],
                }
            )
        except Exception as exc:
            return _err(exc)

    async def app_update_row(
        app_id: Annotated[str, Field(description="App id returned by app_create/app_list.")],
        row_id: Annotated[
            str, Field(description="Row id returned by app_read_data/app_append_row.")
        ],
        row: Annotated[
            dict[str, Any],
            Field(description="Complete row keyed by column name; missing columns become empty."),
        ],
        expected_version: Annotated[
            int | None,
            Field(description="Reject the update when the stored row version differs."),
        ] = None,
        table: Annotated[str, Field(description=_TABLE_HELP)] = MAIN_TABLE_KEY,
    ) -> str:
        try:
            agent_id, user_id = _tool_ctx()
            app = _get_for_actor(service, app_id, agent_id, user_id, "edit_data")
            updated = await service.update_row(
                app,
                row_id,
                row,
                table=table,
                expected_version=expected_version,
            )
            schema = await service.read_schema(app, table)
            return _ok(
                {
                    "app_id": app.app_id,
                    "row_id": updated["row_id"],
                    "version": updated["version"],
                    "pii_columns": [column.name for column in schema.columns if column.pii],
                }
            )
        except Exception as exc:
            return _err(exc)

    async def app_delete_row(
        app_id: Annotated[str, Field(description="App id returned by app_create/app_list.")],
        row_id: Annotated[str, Field(description="Row id returned by app_read_data.")],
        expected_version: Annotated[
            int | None,
            Field(description="Reject the delete when the stored row version differs."),
        ] = None,
    ) -> str:
        try:
            agent_id, user_id = _tool_ctx()
            app = _get_for_actor(service, app_id, agent_id, user_id, "edit_data")
            deleted = await service.delete_row(app, row_id, expected_version=expected_version)
            return _ok({"app_id": app.app_id, "deleted": deleted})
        except Exception as exc:
            return _err(exc)

    async def app_import_rows(
        app_id: Annotated[str, Field(description="App id returned by app_create/app_list.")],
        path: Annotated[
            str,
            Field(
                description=(
                    "Workspace-relative path of the file to import "
                    "(CSV/TSV/JSON/.xlsx/.xls), e.g. inbound/exports/report.csv. "
                    "Hidden files (starting with a dot) are rejected."
                )
            ),
        ] = "",
        paths: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Import several workspace files into the same table, in order. "
                    "Use either path or paths."
                )
            ),
        ] = None,
        table: Annotated[
            str,
            Field(description=_TABLE_HELP),
        ] = MAIN_TABLE_KEY,
        mode: Annotated[
            str,
            Field(
                description="'append' adds rows after the existing ones; 'replace' overwrites the table."
            ),
        ] = "append",
        mapping: Annotated[
            dict[str, str] | None,
            Field(
                description=(
                    "Optional source-column -> table-column map. Default: columns "
                    "with the same name map automatically; others are ignored."
                )
            ),
        ] = None,
        sheet: Annotated[
            str | None,
            Field(
                description=(
                    "Worksheet to import from: a sheet name or a 0-based index. "
                    "Defaults to the first sheet."
                )
            ),
        ] = None,
        header_row: Annotated[
            int | None,
            Field(
                description=(
                    "0-based index of the header line, for files that start with a "
                    "title. Defaults to the first line."
                )
            ),
        ] = None,
        dry_run: Annotated[
            bool,
            Field(
                description=(
                    "Validate and count the rows without writing them. Use it to "
                    "show the user what would land before importing for real."
                )
            ),
        ] = False,
        upsert_key: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Columns whose combined value identifies an existing row; "
                    "matching rows are updated instead of duplicated (e.g. "
                    "['employee_no', 'month'] for a monthly ledger)."
                )
            ),
        ] = None,
        include_pii: Annotated[
            bool,
            Field(
                description=(
                    "Import values of columns detected as sensitive. Default "
                    "false drops them; ask the user before setting true."
                )
            ),
        ] = False,
    ) -> str:
        try:
            agent_id, user_id = _tool_ctx()
            app = _get_for_actor(service, app_id, agent_id, user_id, "edit_data")
            if paths and path:
                raise ValueError("use either path or paths, not both")
            if not paths and not path:
                raise ValueError("an import path is required")
            if paths:
                result = await service.import_from_files(
                    app,
                    paths=paths,
                    table=table,
                    mode=mode,
                    mapping=mapping,
                    sheet=sheet,
                    header_row=header_row,
                    dry_run=dry_run,
                    upsert_key=upsert_key,
                    include_pii=include_pii,
                )
            else:
                result = await service.import_from_file(
                    app,
                    path=path,
                    table=table,
                    mode=mode,
                    mapping=mapping,
                    sheet=sheet,
                    header_row=header_row,
                    dry_run=dry_run,
                    upsert_key=upsert_key,
                    include_pii=include_pii,
                )
            return _ok(
                {
                    "app_id": app.app_id,
                    "table": table,
                    "imported": result.imported,
                    "updated": result.updated,
                    "warnings": result.warnings,
                    "would_import": result.would_import,
                    "dry_run": result.dry_run,
                    "skipped": result.skipped,
                    "errors": result.errors,
                    "pii_columns": result.pii_columns,
                    "blocked_pii": result.blocked_pii,
                    "failed_rows": result.failed_rows,
                }
            )
        except Exception as exc:
            return _err(exc)

    async def app_save_asset(
        app_id: Annotated[str, Field(description="App id returned by app_create/app_list.")],
        source: Annotated[
            str,
            Field(
                description=(
                    "Where to take the image from: an http(s) URL to download, "
                    "or a workspace-relative path (e.g. inbound/cover.jpg)."
                )
            ),
        ],
        name: Annotated[
            str,
            Field(
                description=(
                    "File name to store it under, e.g. cover.jpg. Letters, "
                    "digits, dot, dash and underscore only."
                )
            ),
        ],
    ) -> str:
        try:
            agent_id, user_id = _tool_ctx()
            app = _get_for_actor(service, app_id, agent_id, user_id, "edit_data")
            data = await _read_asset_source(service, app.agent_id, source)
            stored = await service.save_asset(app, name, data)
            return _ok(
                {
                    "app_id": app.app_id,
                    "name": stored,
                    "size": len(data),
                    # Not a fetchable URL: the sandbox has no network. The page
                    # reads the asset through the SDK.
                    "usage": f'octopApp.asset("{stored}")',
                }
            )
        except Exception as exc:
            return _err(exc)

    async def app_set_preview(
        app_id: Annotated[str, Field(description="App id returned by app_create/app_list.")],
        svg: Annotated[
            str,
            Field(
                description=(
                    "The preview image of the app as one self-contained SVG "
                    "document. Use a 1200x630 viewBox, inline styles only, and "
                    "no scripts, no event handlers and no remote references."
                )
            ),
        ],
    ) -> str:
        try:
            agent_id, user_id = _tool_ctx()
            app = _get_for_actor(service, app_id, agent_id, user_id, "edit_data")
            size = await service.write_preview(app, svg)
            return _ok(
                {
                    "app_id": app.app_id,
                    "preview": "preview.svg",
                    "size": size,
                }
            )
        except Exception as exc:
            return _err(exc)

    async def app_delete(
        app_id: Annotated[str, Field(description="App id returned by app_create/app_list.")],
    ) -> str:
        try:
            agent_id, user_id = _tool_ctx()
            row = _get_for_actor(service, app_id, agent_id, user_id, "edit_structure")
            await service.delete(row)
            return _ok({"deleted": row.app_id})
        except Exception as exc:
            return _err(exc)

    return [
        StructuredTool.from_function(
            coroutine=app_inspect_file,
            name="app_inspect_file",
            description=(
                "Inspect a spreadsheet file in the workspace and return a compact report: "
                "sheets, header row, per-column type/emptiness/distinct counts, sample "
                "values and PII warnings. Use it before creating an app from a ledger — "
                "never read the file row by row."
            ),
        ),
        StructuredTool.from_function(
            coroutine=app_list,
            name="app_list",
            description=(
                "List the lightweight apps owned by the current user and created by this agent. "
                "Returns app_id, name, description and row_count."
            ),
        ),
        StructuredTool.from_function(
            coroutine=app_open,
            name="app_open",
            description=(
                "Open an existing lightweight app in the chat. Call this when the user "
                "asks to open, show or use an app; the dashboard renders an app card "
                "with an inline preview and an open button. On IM channels this "
                "returns a public link + plain text instead."
            ),
        ),
        StructuredTool.from_function(
            coroutine=app_share_link,
            name="app_share_link",
            description=(
                "Get a channel-ready entry for a lightweight app in ONE call: reuses "
                "or creates its read-only public link, returns a plain-text message "
                "and (when available) a cover. Use this for chat/IM requests such "
                "as 'give me the entry/link/card for X'. The result may include a "
                "sharing notice (public/private tables, masked or unmarked PII); "
                "relay that notice to the user in their language before sharing. "
                "Treat the returned text as the final answer: do not browse, "
                "screenshot, curl, query the database, read source code, download "
                "or self-host the app afterwards."
            ),
        ),
        StructuredTool.from_function(
            coroutine=app_create,
            name="app_create",
            description=(
                "Create a lightweight app (single-file HTML UI backed by a CSV table) in the "
                "agent workspace. Define 1-50 columns up front, then call app_write_html to "
                "build the interface and app_append_row to add data. Use this when the user "
                "asks for a small tracker, dashboard, list or calculator-like tool."
            ),
        ),
        StructuredTool.from_function(
            coroutine=app_create_from_template,
            name="app_create_from_template",
            description=(
                "Create an empty app from an expert-provided app template "
                "(schema + HTML shell). The template carries no user data; use it "
                "when the user asks for a standard tool such as a ledger or tracker."
            ),
        ),
        StructuredTool.from_function(
            coroutine=app_add_table,
            name="app_add_table",
            description=(
                "Add another table (its own CSV and columns) to an existing lightweight "
                "app. Use it when one app needs several datasets, e.g. a todo list and a "
                "list of projects."
            ),
        ),
        StructuredTool.from_function(
            coroutine=app_update_schema,
            name="app_update_schema",
            description=(
                "Append columns to an existing lightweight app. Existing data is preserved; "
                "new cells start empty. Use this instead of deleting and recreating the app."
            ),
        ),
        StructuredTool.from_function(
            coroutine=app_alter_schema,
            name="app_alter_schema",
            description=(
                "Rename, drop or retype columns of a lightweight app. Every "
                "existing row is migrated; the change is rejected when a row "
                "does not fit the new schema."
            ),
        ),
        StructuredTool.from_function(
            coroutine=app_set_table_visibility,
            name="app_set_table_visibility",
            description=(
                "Expose or hide one table on the app's public read-only link. The main "
                "table is public by default; add-on tables are private. Call this before "
                "sharing when the public link should include (or must exclude) a table."
            ),
        ),
        StructuredTool.from_function(
            coroutine=app_set_capabilities,
            name="app_set_capabilities",
            description=(
                "Replace the capability manifest that gates what the sandboxed app page "
                "may request from the host. Use it to remove high-risk actions the app "
                "does not need, or to grant ai.generate / game.launch / external.fetch / "
                "events.emit / schedules.manage when the app really uses them."
            ),
        ),
        StructuredTool.from_function(
            coroutine=app_write_html,
            name="app_write_html",
            description=(
                "Write the complete HTML UI for a lightweight app, replacing the previous "
                "version. The page can read and append rows through window.octopApp."
            ),
        ),
        StructuredTool.from_function(
            coroutine=app_read_data,
            name="app_read_data",
            description=(
                "Read a page of rows from a lightweight app together with its column "
                "definitions. `row_meta` is parallel to `rows` and carries the row_id "
                "and version needed by app_update_row / app_delete_row."
            ),
        ),
        StructuredTool.from_function(
            coroutine=app_append_row,
            name="app_append_row",
            description=(
                "Append one validated row to a lightweight app. Values are coerced to the "
                "column type; enums must match one of their declared values."
            ),
        ),
        StructuredTool.from_function(
            coroutine=app_update_row,
            name="app_update_row",
            description=(
                "Replace one row of a lightweight app. Pass expected_version to "
                "reject a stale edit instead of overwriting another user's change."
            ),
        ),
        StructuredTool.from_function(
            coroutine=app_delete_row,
            name="app_delete_row",
            description=(
                "Delete one row of a lightweight app. Pass expected_version to "
                "reject a stale delete."
            ),
        ),
        StructuredTool.from_function(
            coroutine=app_import_rows,
            name="app_import_rows",
            description=(
                "Import rows from a workspace file (CSV/TSV/JSON/.xlsx/.xls) into a "
                "lightweight app table. Rows that fail validation are skipped and "
                "counted. Use it to bulk-load data the user provided or exported."
            ),
        ),
        StructuredTool.from_function(
            coroutine=app_save_asset,
            name="app_save_asset",
            description=(
                "Store an image next to the app (a book cover, an icon). The app "
                "page cannot load remote images, so covers have to be saved "
                "here and shown through octopApp.asset(name). PNG, JPEG, WebP "
                "or GIF, at most 5 MB."
            ),
        ),
        StructuredTool.from_function(
            coroutine=app_set_preview,
            name="app_set_preview",
            description=(
                "Set the preview image (cover) of a lightweight app. The preview "
                "is one self-contained SVG document that the app list shows as "
                "the app's thumbnail. Draw it yourself with a 1200x630 viewBox, "
                "inline styles only, no scripts, no event handlers and no "
                "remote references. Call it once the app's look is settled."
            ),
        ),
        StructuredTool.from_function(
            coroutine=app_delete,
            name=APP_DELETE_TOOL_NAME,
            description=(
                "Permanently delete a lightweight app and all of its data. The user must "
                "approve this call before it runs."
            ),
        ),
    ]
