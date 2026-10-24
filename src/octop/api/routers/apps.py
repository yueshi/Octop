"""HTTP API for agent-built light apps.

Apps are created and edited by agents through built-in tools; this router only
exposes them so the dashboard and collaborators can render the UI and append
rows. Ownership and sharing are enforced through ``AppService``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from octop.api.common.upload_limit import read_upload_capped
from octop.api.deps import current_user, get_server
from octop.api.routers.chat.sse import format_sse
from octop.config import DEFAULT_MAX_UPLOAD_MB, upload_mb_to_bytes
from octop.infra.apps.assistant import AppAssistant
from octop.infra.apps.models import MAIN_TABLE_KEY, Schema
from octop.infra.apps.service import (
    IMPORT_MODE_REPLACE,
    AppRecord,
    AppService,
    AppTableView,
    ImportResult,
    SourceView,
)
from octop.infra.apps.sources import GatewayToolRunner
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.server import OctopServer
from octop.infra.users.identity import User
from octop.infra.utils.locale import Locale, resolve_request_locale

router = APIRouter(prefix="/apps")

SharePermission = Literal["view", "edit_data"]


class AppColumn(BaseModel):
    name: str = Field(description="Machine name used as the CSV column and row key.")
    label: str = Field(description="Human-readable column label.")
    type: str = Field(description="Value type (string, integer, boolean, date, enum, …).")
    required: bool = Field(default=False, description="Whether the column must be non-empty.")
    values: list[str] = Field(default_factory=list, description="Allowed values for enum columns.")


class AppSummary(BaseModel):
    app_id: str
    name: str
    description: str
    agent_id: str = Field(description="Agent that owns the app workspace.")
    agent_name: str = ""
    owner_user_id: int
    owner_username: str = ""
    permission: str = Field(description="Caller's permission: view, edit_data or edit_structure.")
    row_count: int = Field(description="Rows across every table of the app.")
    html_bytes: int = Field(default=0, description="Size of the generated app.html in bytes.")
    data_bytes: int = Field(
        default=0,
        description="Size of the app's CSV files in bytes, summed over every table.",
    )
    has_html: bool = Field(default=False, description="Whether the agent has written the UI yet.")
    created_at: int
    updated_at: int


class AppFileOut(BaseModel):
    name: str = Field(description="File name inside the app directory.")
    path: str = Field(description="Workspace-relative path of the file.")
    size: int | None = Field(
        default=None, description="File size in bytes, when the backend reports it."
    )


class AppTableOut(BaseModel):
    table_key: str = Field(description="Machine key of the table.")
    label: str = Field(default="", description="Display name of the table.")
    row_count: int = Field(default=0, description="Rows currently stored in the table.")
    is_main: bool = Field(default=False, description="Whether this is the app's main table.")
    columns: list[AppColumn] = Field(
        default_factory=list,
        description="Column definitions of the table.",
    )


class AppTablesResponse(BaseModel):
    tables: list[AppTableOut]


class AppFilesResponse(BaseModel):
    files: list[AppFileOut]


class AppListResponse(BaseModel):
    apps: list[AppSummary]


class AppDetail(AppSummary):
    columns: list[AppColumn] = Field(description="Columns of the main table, in display order.")
    has_html: bool = Field(description="Whether the agent has written app.html yet.")
    tables: list[AppTableOut] = Field(
        default_factory=list,
        description="Tables of the app; the first one is the main table.",
    )


class AppRowsResponse(BaseModel):
    columns: list[AppColumn]
    rows: list[dict[str, str]]
    total: int
    limit: int
    offset: int
    table: str = Field(default="main", description="Table the rows came from.")


class AppendRowBody(BaseModel):
    row: dict[str, Any] = Field(description="One row keyed by column name.")
    table: str = Field(default="main", description="Table to append to.")


class AppendRowResponse(BaseModel):
    row_index: int = Field(description="Zero-based index of the appended row.")
    row: dict[str, str]
    table: str = Field(default="main", description="Table the row was appended to.")


class UpdateAppBody(BaseModel):
    name: str | None = Field(default=None, description="New display name.")
    description: str | None = Field(default=None, description="New description.")


class DeleteAppResponse(BaseModel):
    deleted: str


class ShareEntry(BaseModel):
    username: str
    permission: SharePermission = "view"


class SetSharesBody(BaseModel):
    shares: list[ShareEntry] = Field(description="Replacement collaborator list.")


class ShareOut(BaseModel):
    user_id: int
    username: str
    display_name: str
    permission: str


class SharesResponse(BaseModel):
    shares: list[ShareOut]


def _service(server: OctopServer) -> AppService:
    if server.services is None or server.app_runtime is None:
        raise OctopError(ErrorCode.INTERNAL_ERROR, "server is not started")
    return AppService(
        app_repo=server.services.light_app_repo,
        agent_repo=server.services.agent_repo,
        user_repo=server.services.user_repo,
        workspaces=server.app_runtime.agent_registry,
    )


def _columns(schema: Schema) -> list[AppColumn]:
    return [
        AppColumn(
            name=col.name,
            label=col.label,
            type=col.type,
            required=col.required,
            values=list(col.values),
        )
        for col in schema.columns
    ]


def _summary(service: AppService, row: AppRecord, permission: str) -> AppSummary:
    view = service.view(row, permission)
    return AppSummary(**asdict(view), has_html=view.has_html)


def _summary_from_view(view: Any) -> AppSummary:
    return AppSummary(**asdict(view), has_html=view.has_html)


async def _table_out(service: AppService, row: AppRecord, table: AppTableView) -> AppTableOut:
    """Table metadata plus its column definitions (read from the workspace)."""
    columns: list[AppColumn] = []
    try:
        schema = await service.read_schema(row, table.table_key)
    except OctopError:
        schema = None
    if schema is not None:
        columns = _columns(schema)
    return AppTableOut(
        table_key=table.table_key,
        label=table.label,
        row_count=table.row_count,
        is_main=table.is_main,
        columns=columns,
    )


async def _table_outs(service: AppService, row: AppRecord) -> list[AppTableOut]:
    return [await _table_out(service, row, table) for table in service.list_tables(row)]


def _row_access(
    server: OctopServer,
    user: User,
    app_id: str,
    needed: str,
) -> tuple[AppService, AppRecord, str]:
    service = _service(server)
    row = service.get(app_id)
    permission = service.require(row, user.id, needed, is_admin=user.is_admin)
    return service, row, permission


def _assistant(server: OctopServer, service: AppService) -> AppAssistant:
    if server.services is None or server.app_runtime is None:
        raise OctopError(ErrorCode.INTERNAL_ERROR, "server is not started")
    return AppAssistant(
        service=service,
        app_repo=server.services.light_app_repo,
        agents=server.app_runtime.agent_registry,
        usage_repo=server.services.usage_repo,
    )


def _tool_runner(server: OctopServer) -> GatewayToolRunner:
    if server.services is None:
        raise OctopError(ErrorCode.INTERNAL_ERROR, "server is not started")
    from octop.infra.connectors.service import ConnectorService  # noqa: PLC0415

    return GatewayToolRunner(
        repo=server.services.repos.connector_repo,
        service=ConnectorService(
            repo=server.services.repos.connector_repo,
            secret_repo=server.services.secret_repo,
            settings_repo=server.services.settings_repo,
            config=server.services.config,
        ),
    )


def _service_with_runner(server: OctopServer) -> AppService:
    """AppService wired with the gateway tool runner (external sources)."""
    if server.services is None or server.app_runtime is None:
        raise OctopError(ErrorCode.INTERNAL_ERROR, "server is not started")
    return AppService(
        app_repo=server.services.light_app_repo,
        agent_repo=server.services.agent_repo,
        user_repo=server.services.user_repo,
        workspaces=server.app_runtime.agent_registry,
        external_tool_runner=_tool_runner(server),
    )


@router.get(
    "",
    response_model=AppListResponse,
    summary="List light apps",
    description="Apps owned by the caller plus apps shared with them. Admins see every app.",
)
async def list_apps(
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> AppListResponse:
    service = _service(server)
    return AppListResponse(
        apps=[
            _summary_from_view(view)
            for view in await service.list_views_with_stats(user.id, is_admin=user.is_admin)
        ]
    )


@router.get(
    "/{app_id}",
    response_model=AppDetail,
    summary="Get light app details",
)
async def get_app(
    app_id: str,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> AppDetail:
    service, row, permission = _row_access(server, user, app_id, "view")
    schema = await service.read_schema(row)
    base = _summary(service, row, permission)
    return AppDetail(
        # The detail view checks the workspace directly: the cached size is a
        # hint for list views, the file itself is the truth.
        **base.model_dump(exclude={"has_html"}),
        columns=_columns(schema),
        has_html=await service.has_html(row),
        tables=await _table_outs(service, row),
    )


@router.get(
    "/{app_id}/tables",
    response_model=AppTablesResponse,
    summary="List app tables",
    description=(
        "Lists every table of the app (the main table first) with its columns and "
        "row count. Use the returned keys with the rows endpoints."
    ),
)
async def list_app_tables(
    app_id: str,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> AppTablesResponse:
    service, row, _permission = _row_access(server, user, app_id, "view")
    return AppTablesResponse(tables=await _table_outs(service, row))


@router.get(
    "/{app_id}/html",
    summary="Get the app HTML",
    description=(
        "Returns the single-file HTML written by the agent. Render it in a sandboxed "
        'iframe (`sandbox="allow-scripts"`); an empty body means no UI has been generated yet.'
    ),
    responses={200: {"content": {"text/html": {}}}},
)
async def get_app_html(
    app_id: str,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> Response:
    service, row, _permission = _row_access(server, user, app_id, "view")
    html = await service.read_html(row)
    # The HTML is agent-generated. Render it in the dashboard's sandboxed
    # iframe; a direct visit must not run its scripts on the API origin, so
    # mirror the workspace preview sandbox headers.
    return Response(
        content=html or "",
        media_type="text/html; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": "inline",
            "Content-Security-Policy": "sandbox",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get(
    "/{app_id}/files",
    response_model=AppFilesResponse,
    summary="List app files",
    description=(
        "Lists the files that make up the app (interface, every table's CSV and "
        "schema, and the manifest) with their sizes, so the owner can see where "
        "the app lives."
    ),
)
async def list_app_files(
    app_id: str,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> AppFilesResponse:
    service, row, _permission = _row_access(server, user, app_id, "view")
    files = await service.list_files(row)
    return AppFilesResponse(
        files=[AppFileOut(name=item.name, path=item.path, size=item.size) for item in files],
    )


@router.get(
    "/{app_id}/rows",
    response_model=AppRowsResponse,
    summary="Read app rows",
)
async def get_app_rows(
    app_id: str,
    limit: int = Query(200, ge=1, le=500, description="Rows to return."),
    offset: int = Query(0, ge=0, description="Rows to skip."),
    table: str = Query(MAIN_TABLE_KEY, description="Table key to read from."),
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> AppRowsResponse:
    service, row, _permission = _row_access(server, user, app_id, "view")
    schema, rows, total = await service.read_rows(row, limit=limit, offset=offset, table=table)
    return AppRowsResponse(
        columns=_columns(schema),
        rows=rows,
        total=total,
        limit=limit,
        offset=offset,
        table=table,
    )


@router.post(
    "/{app_id}/rows",
    response_model=AppendRowResponse,
    summary="Append a row",
    description="Requires the edit_data or edit_structure permission.",
)
async def append_app_row(
    app_id: str,
    body: AppendRowBody,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> AppendRowResponse:
    service, row, _permission = _row_access(server, user, app_id, "edit_data")
    index, stored = await service.append_row(row, body.row, table=body.table)
    return AppendRowResponse(row_index=index, row=stored, table=body.table)


@router.patch(
    "/{app_id}",
    response_model=AppDetail,
    summary="Update app name or description",
    description="Requires the edit_structure permission (owner or admin).",
)
async def update_app(
    app_id: str,
    body: UpdateAppBody,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> AppDetail:
    service, row, permission = _row_access(server, user, app_id, "edit_structure")
    row = await service.update_metadata(row, name=body.name, description=body.description)
    schema = await service.read_schema(row)
    base = _summary(service, row, permission)
    return AppDetail(
        **base.model_dump(exclude={"has_html"}),
        columns=_columns(schema),
        has_html=await service.has_html(row),
        tables=await _table_outs(service, row),
    )


@router.delete(
    "/{app_id}",
    response_model=DeleteAppResponse,
    summary="Delete a light app",
    description="Deletes the app files and metadata. Requires edit_structure.",
)
async def delete_app(
    app_id: str,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> DeleteAppResponse:
    service, row, _permission = _row_access(server, user, app_id, "edit_structure")
    await service.delete(row)
    return DeleteAppResponse(deleted=row.app_id)


@router.get(
    "/{app_id}/shares",
    response_model=SharesResponse,
    summary="List collaborators",
    description="Requires edit_structure.",
)
async def list_app_shares(
    app_id: str,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> SharesResponse:
    service, row, _permission = _row_access(server, user, app_id, "edit_structure")
    return SharesResponse(
        shares=[ShareOut(**asdict(view)) for view in service.list_share_views(row)]
    )


@router.put(
    "/{app_id}/shares",
    response_model=SharesResponse,
    summary="Replace collaborators",
    description="Replaces the full collaborator list. Requires edit_structure.",
)
async def set_app_shares(
    app_id: str,
    body: SetSharesBody,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> SharesResponse:
    service, row, _permission = _row_access(server, user, app_id, "edit_structure")
    views = service.set_shares(row, [(share.username, share.permission) for share in body.shares])
    return SharesResponse(shares=[ShareOut(**asdict(view)) for view in views])


class AskAssistantBody(BaseModel):
    question: str = Field(
        min_length=1,
        max_length=4000,
        description="The question about this app's data.",
    )


class AssistantMessageOut(BaseModel):
    role: str = Field(description="user or assistant.")
    content: str
    created_at: int


class AssistantHistoryResponse(BaseModel):
    messages: list[AssistantMessageOut] = Field(
        description="The caller's conversation with this app's assistant, oldest first.",
    )


@router.get(
    "/{app_id}/assistant/history",
    response_model=AssistantHistoryResponse,
    summary="Get app assistant history",
    description="The caller's own conversation with this app's assistant, oldest first.",
)
async def get_app_assistant_history(
    app_id: str,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> AssistantHistoryResponse:
    service, row, _permission = _row_access(server, user, app_id, "view")
    turns = _assistant(server, service).history(row, user.id)
    return AssistantHistoryResponse(
        messages=[
            AssistantMessageOut(role=t.role, content=t.content, created_at=t.created_at)
            for t in turns
        ],
    )


@router.post(
    "/{app_id}/assistant",
    summary="Ask the app assistant (SSE)",
    description=(
        "Streams SSE frames (event: chunk) whose data carries "
        "{type: token|tool|done|error}. Read-only over the app's data; consumes "
        "the owning agent's model quota. Requires the view permission."
    ),
)
async def ask_app_assistant(
    app_id: str,
    body: AskAssistantBody,
    request: Request,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> StreamingResponse:
    service, row, _permission = _row_access(server, user, app_id, "view")
    question = body.question.strip()
    if not question:
        raise OctopError(ErrorCode.APP_QUESTION_INVALID, "question must not be empty")
    assistant = _assistant(server, service)
    locale: Locale = resolve_request_locale(request)
    # Resolve the agent and model before streaming so availability problems
    # (agent not running, no usable model) surface as regular HTTP errors.
    prepared = assistant.prepare(row, user_id=user.id, question=question, locale=locale)

    async def gen() -> AsyncIterator[str]:
        async for event in assistant.stream(prepared):
            yield format_sse("chunk", event)

    return StreamingResponse(gen(), media_type="text/event-stream")


class UploadImportResponse(BaseModel):
    path: str = Field(description="Workspace-relative path to pass to the import endpoint.")
    filename: str
    size: int


class ImportBody(BaseModel):
    kind: Literal["upload", "workspace_file"] = Field(
        description=(
            "upload: a file previously stored via the upload endpoint; "
            "workspace_file: an existing workspace file (owner/admin only)."
        ),
    )
    path: str = Field(description="Workspace-relative path of the file to import.")
    mode: Literal["append", "replace"] = Field(
        default="append",
        description="append adds rows after the existing ones; replace overwrites the table.",
    )
    mapping: dict[str, str] | None = Field(
        default=None,
        description=(
            "Optional source-column -> table-column map; unmapped source columns "
            "are ignored, unmapped table columns are left empty."
        ),
    )


class ImportResponse(BaseModel):
    table: str
    imported: int = Field(description="Rows written into the table.")
    skipped: int = Field(description="Rows dropped because they failed validation.")
    errors: list[str] = Field(description="Up to 5 row-level validation failures.")


@router.post(
    "/{app_id}/upload",
    response_model=UploadImportResponse,
    summary="Upload an import file",
    description=(
        "Stores a CSV/TSV/JSON/Excel file under the agent workspace "
        "``inbound/imports/`` and returns its path for the import endpoint. "
        "Requires the edit_data permission."
    ),
)
async def upload_app_import_file(
    app_id: str,
    file: UploadFile = File(...),  # noqa: B008
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> UploadImportResponse:
    service, row, _permission = _row_access(server, user, app_id, "edit_data")
    max_bytes = (
        int(server.services.config.max_upload_bytes) if server.services is not None else None
    )
    fallback = upload_mb_to_bytes(DEFAULT_MAX_UPLOAD_MB)
    data = await read_upload_capped(file, max_bytes=max_bytes or fallback)
    path = await service.save_import_file(row, filename=file.filename or "upload.bin", data=data)
    return UploadImportResponse(path=path, filename=file.filename or "upload.bin", size=len(data))


@router.post(
    "/{app_id}/tables/{table_key}/import",
    response_model=ImportResponse,
    summary="Import rows into a table",
    description=(
        "Parses a CSV/TSV/JSON/Excel file and writes its rows into one table of "
        "the app. Rows that fail validation are skipped and counted. append "
        "requires edit_data; replace and workspace-file sources require "
        "edit_structure (owner or admin)."
    ),
)
async def import_app_table(
    app_id: str,
    table_key: str,
    body: ImportBody,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> ImportResponse:
    needed = (
        "edit_structure"
        if body.mode == IMPORT_MODE_REPLACE or body.kind == "workspace_file"
        else "edit_data"
    )
    service, row, _permission = _row_access(server, user, app_id, needed)
    result: ImportResult = await service.import_from_file(
        row,
        path=body.path,
        table=table_key,
        mode=body.mode,
        mapping=body.mapping,
    )
    return ImportResponse(
        table=table_key,
        imported=result.imported,
        skipped=result.skipped,
        errors=result.errors,
    )


class SourceOut(BaseModel):
    source_id: str
    table_key: str = Field(description="Table the source feeds.")
    kind: str = Field(description="workspace_file or external.")
    mode: str = Field(description="append or replace.")
    schedule_seconds: int = Field(description="0 means manual-only.")
    params: dict[str, Any] = Field(
        description="workspace_file: {path}; external: {instance_id, tool, args?, result_path?}.",
    )
    mapping: dict[str, str] = Field(description="Source-column to table-column map.")
    last_run_at: int | None = None
    last_status: str | None = None
    last_error: str | None = None
    last_row_count: int | None = None
    created_by: int
    created_at: int
    updated_at: int


class SourcesResponse(BaseModel):
    sources: list[SourceOut]


class SaveSourceBody(BaseModel):
    table_key: str = Field(description="Table the source feeds.")
    kind: Literal["workspace_file", "external"]
    mode: Literal["append", "replace"] = "append"
    schedule_seconds: int = Field(
        default=0,
        ge=0,
        description="Auto-sync interval; 0 means manual-only (scheduler ships with M5).",
    )
    params: dict[str, Any] = Field(default_factory=dict)
    mapping: dict[str, str] = Field(default_factory=dict)


class SourceToolsOut(BaseModel):
    name: str
    description: str = ""
    read_only: bool = Field(
        description="Whether the tool declares readOnlyHint (or declares nothing)."
    )
    input_schema: dict[str, Any] = Field(default_factory=dict)


class SourceToolsResponse(BaseModel):
    tools: list[SourceToolsOut]


class SourceRunResponse(BaseModel):
    table: str
    imported: int
    skipped: int
    errors: list[str]


def _source_out(view: SourceView) -> SourceOut:
    return SourceOut(
        source_id=view.source_id,
        table_key=view.table_key,
        kind=view.kind,
        mode=view.mode,
        schedule_seconds=view.schedule_seconds,
        params=view.params,
        mapping=view.mapping,
        last_run_at=view.last_run_at,
        last_status=view.last_status,
        last_error=view.last_error,
        last_row_count=view.last_row_count,
        created_by=view.created_by,
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


@router.get(
    "/{app_id}/sources",
    response_model=SourcesResponse,
    summary="List data-source bindings",
)
async def list_app_sources(
    app_id: str,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> SourcesResponse:
    service, row, _permission = _row_access(server, user, app_id, "view")
    return SourcesResponse(sources=[_source_out(view) for view in service.list_sources(row)])


@router.put(
    "/{app_id}/sources/{source_id}",
    response_model=SourceOut,
    summary="Create or update a data-source binding",
    description=(
        "Binds one source to one table (a table holds at most one binding). "
        "Requires edit_structure. External sources run with the app owner's "
        "connector credentials."
    ),
)
async def save_app_source(
    app_id: str,
    source_id: str,
    body: SaveSourceBody,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> SourceOut:
    service, row, _permission = _row_access(server, user, app_id, "edit_structure")
    if body.kind == "external":
        # Bind-time validation (defense in depth: the runner re-checks
        # read-only on every call).
        instance_id = str(body.params.get("instance_id") or "")
        tool = str(body.params.get("tool") or "")
        meta = _tool_runner(server).tool_meta(instance_id, tool)
        if meta is None:
            raise OctopError(
                ErrorCode.APP_SOURCE_INVALID,
                f"tool {tool!r} is not available on connector {instance_id!r}",
            )
        if not meta["read_only"]:
            raise OctopError(
                ErrorCode.APP_SOURCE_INVALID,
                f"tool {meta['name']!r} is not read-only and cannot back a data source",
            )
    view = service.save_source(
        row,
        source_id=source_id,
        table_key=body.table_key,
        kind=body.kind,
        mode=body.mode,
        schedule_seconds=body.schedule_seconds,
        params=body.params,
        mapping=body.mapping,
        created_by=user.id,
    )
    return _source_out(view)


@router.delete(
    "/{app_id}/sources/{source_id}",
    status_code=204,
    summary="Delete a data-source binding",
)
async def delete_app_source(
    app_id: str,
    source_id: str,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> Response:
    service, row, _permission = _row_access(server, user, app_id, "edit_structure")
    service.delete_source(row, source_id)
    return Response(status_code=204)


@router.post(
    "/{app_id}/sources/{source_id}/run",
    response_model=SourceRunResponse,
    summary="Run a data source now",
    description="Re-imports from the bound source into its table. Requires edit_data.",
)
async def run_app_source(
    app_id: str,
    source_id: str,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> SourceRunResponse:
    service, row, _permission = _row_access(server, user, app_id, "edit_data")
    view = service.get_source(row, source_id)
    if view.kind == "external":
        service = _service_with_runner(server)
    result = await service.run_source(row, source_id)
    return SourceRunResponse(
        table=view.table_key,
        imported=result.imported,
        skipped=result.skipped,
        errors=result.errors,
    )


@router.get(
    "/{app_id}/source-tools",
    response_model=SourceToolsResponse,
    summary="List connector tools usable as data sources",
    description=(
        "Tools of one in-process gateway connector instance (query: instance_id), "
        "for the binding dialog. Requires edit_structure."
    ),
)
async def list_app_source_tools(
    app_id: str,
    instance_id: str = Query(description="Connector instance id."),
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> SourceToolsResponse:
    _row_access(server, user, app_id, "edit_structure")
    runner = _tool_runner(server)
    return SourceToolsResponse(
        tools=[
            SourceToolsOut(
                name=str(meta["name"]),
                description=str(meta.get("description") or ""),
                read_only=bool(meta["read_only"]),
                input_schema=dict(meta.get("input_schema") or {}),
            )
            for meta in runner.list_tool_meta(instance_id)
        ]
    )
