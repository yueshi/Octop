"""Built-in LangChain tools for agent-managed light apps."""

from __future__ import annotations

import json
from typing import Annotated, Any

from langchain_core.tools import StructuredTool
from langgraph.config import get_config
from pydantic import Field

from octop.infra.agents.tool_scope import current_tool_scope
from octop.infra.apps.models import MAIN_TABLE_KEY
from octop.infra.apps.service import AppService, AppTableView
from octop.infra.db.repos.apps import LightAppRow

APP_DELETE_TOOL_NAME = "app_delete"

APP_TOOL_NAMES: tuple[str, ...] = (
    "app_list",
    "app_create",
    "app_add_table",
    "app_update_schema",
    "app_write_html",
    "app_read_data",
    "app_append_row",
    "app_import_rows",
    APP_DELETE_TOOL_NAME,
)
"""Names of the built-in light-app tools, in the order they are registered."""

_COLUMNS_HELP = (
    "Column definitions. Each item: "
    '{"name": "snake_case identifier", "label": "display label", '
    '"type": "string|text|integer|number|boolean|date|datetime|enum", '
    '"required": false, "values": ["only for enum"]}. '
    "Up to 50 columns; names must start with a letter or underscore."
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


def _ok(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _err(exc: Exception) -> str:
    return json.dumps({"error": str(exc)}, ensure_ascii=False)


def _get_owned(service: AppService, app_id: str, agent_id: str, user_id: int) -> LightAppRow:
    if not service.is_agent_owner(agent_id, user_id):
        raise ValueError("light apps can only be managed by the agent owner")
    row = service.get(app_id)
    if row.agent_id != agent_id or row.owner_user_id != user_id:
        raise ValueError(f"light app not found: {app_id}")
    return row


def _summary(row: LightAppRow) -> dict[str, Any]:
    return {
        "app_id": row.app_id,
        "name": row.name,
        "description": row.description,
        "row_count": row.row_count,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _table_summary(table: AppTableView) -> dict[str, Any]:
    return {
        "table_key": table.table_key,
        "label": table.label,
        "row_count": table.row_count,
        "is_main": table.is_main,
    }


def build_app_tools(service: AppService) -> list[StructuredTool]:
    """Return built-in light-app tools (wired via ``HarnessAgentConfig.tools``)."""

    async def app_list() -> str:
        try:
            agent_id, user_id = _tool_ctx()
            if not service.is_agent_owner(agent_id, user_id):
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
    ) -> str:
        try:
            agent_id, user_id = _tool_ctx()
            if not service.is_agent_owner(agent_id, user_id):
                raise ValueError("light apps can only be managed by the agent owner")
            row = await service.create(
                agent_id=agent_id,
                owner_user_id=user_id,
                name=name,
                description=description,
                columns=columns,
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
            row = _get_owned(service, app_id, agent_id, user_id)
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

    async def app_write_html(
        app_id: Annotated[str, Field(description="App id returned by app_create/app_list.")],
        html: Annotated[str, Field(description=_HTML_HELP)],
    ) -> str:
        try:
            agent_id, user_id = _tool_ctx()
            row = _get_owned(service, app_id, agent_id, user_id)
            size = await service.write_html(row, html)
            return _ok({"app_id": row.app_id, "bytes": size})
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
    ) -> str:
        try:
            agent_id, user_id = _tool_ctx()
            row = _get_owned(service, app_id, agent_id, user_id)
            table = await service.create_table(
                row,
                table_key=key,
                label=label,
                columns=columns,
            )
            return _ok(
                {
                    "app_id": row.app_id,
                    "table": table.table_key,
                    "label": table.label,
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
            row = _get_owned(service, app_id, agent_id, user_id)
            schema, rows, total = await service.read_rows(
                row,
                limit=limit,
                offset=offset,
                table=table,
            )
            return _ok(
                {
                    "app_id": row.app_id,
                    "table": table,
                    "columns": schema.to_dict()["columns"],
                    "rows": rows,
                    "total": total,
                    "limit": limit,
                    "offset": offset,
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
    ) -> str:
        try:
            agent_id, user_id = _tool_ctx()
            app = _get_owned(service, app_id, agent_id, user_id)
            index, stored = await service.append_row(app, row, table=table)
            return _ok(
                {
                    "app_id": app.app_id,
                    "table": table,
                    "row_index": index,
                    "row": stored,
                }
            )
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
        ],
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
    ) -> str:
        try:
            agent_id, user_id = _tool_ctx()
            app = _get_owned(service, app_id, agent_id, user_id)
            result = await service.import_from_file(
                app,
                path=path,
                table=table,
                mode=mode,
                mapping=mapping,
            )
            return _ok(
                {
                    "app_id": app.app_id,
                    "table": table,
                    "imported": result.imported,
                    "skipped": result.skipped,
                    "errors": result.errors,
                }
            )
        except Exception as exc:
            return _err(exc)

    async def app_delete(
        app_id: Annotated[str, Field(description="App id returned by app_create/app_list.")],
    ) -> str:
        try:
            agent_id, user_id = _tool_ctx()
            row = _get_owned(service, app_id, agent_id, user_id)
            await service.delete(row)
            return _ok({"deleted": row.app_id})
        except Exception as exc:
            return _err(exc)

    return [
        StructuredTool.from_function(
            coroutine=app_list,
            name="app_list",
            description=(
                "List the lightweight apps owned by the current user and created by this agent. "
                "Returns app_id, name, description and row_count."
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
                "definitions. Use it before answering questions about the app's data."
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
            coroutine=app_import_rows,
            name="app_import_rows",
            description=(
                "Import rows from a workspace file (CSV/TSV/JSON/.xlsx/.xls) into a "
                "lightweight app table. Rows that fail validation are skipped and "
                "counted. Use it to bulk-load data the user provided or exported."
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
