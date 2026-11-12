"""HTTP API for agent-built MiniApps.

Apps are created and edited by agents through built-in tools; this router only
exposes them so the dashboard and collaborators can render the UI and append
rows. Ownership and sharing are enforced through ``AppService``.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from octop.api.common.agent import require_agent_row
from octop.api.common.upload_limit import read_upload_capped
from octop.api.deps import current_user, get_server
from octop.api.routers.chat.sse import format_sse
from octop.config import DEFAULT_MAX_UPLOAD_MB, upload_mb_to_bytes
from octop.infra.apps import capabilities as app_capabilities
from octop.infra.apps.assistant import AppAssistant
from octop.infra.apps.game_sessions import create_game_token
from octop.infra.apps.generation import AppGenerator
from octop.infra.apps.models import MAIN_TABLE_KEY, Schema
from octop.infra.apps.preview import validate_preview_svg
from octop.infra.apps.service import (
    IMPORT_MODE_REPLACE,
    MAX_ASSET_BYTES,
    MAX_BACKUP_BYTES,
    AppRecord,
    AppService,
    AppTableView,
    ImportResult,
    SourceView,
)
from octop.infra.apps.session import app_card_ids
from octop.infra.apps.sources import GatewayToolRunner
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.server import OctopServer
from octop.infra.users.identity import User
from octop.infra.utils.locale import Locale, resolve_request_locale
from octop.infra.utils.ulid import new_short_id

router = APIRouter(prefix="/apps")

SharePermission = Literal["view", "edit_data"]


class AppColumn(BaseModel):
    name: str = Field(description="Machine name used as the CSV column and row key.")
    label: str = Field(description="Human-readable column label.")
    type: str = Field(description="Value type (string, integer, boolean, date, enum, …).")
    required: bool = Field(default=False, description="Whether the column must be non-empty.")
    values: list[str] = Field(default_factory=list, description="Allowed values for enum columns.")
    pii: str | None = Field(
        default=None,
        description="Sensitive-data kind; values are masked for viewers and public links.",
    )


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
    has_preview: bool = Field(
        default=False, description="Whether the agent drew a preview image for the app."
    )
    capabilities: list[str] = Field(
        default_factory=list,
        description="Page-facing capabilities the MiniApp may request from the host.",
    )
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
    public_visible: bool = Field(
        default=True,
        description="Whether the table is exposed by the app's public read-only link.",
    )
    columns: list[AppColumn] = Field(
        default_factory=list,
        description="Column definitions of the table.",
    )


class AppTablesResponse(BaseModel):
    tables: list[AppTableOut]


class TableVisibilityBody(BaseModel):
    public_visible: bool = Field(
        description="Expose this table on the app's public read-only link."
    )


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


class FailedRowOut(BaseModel):
    row: int = Field(description="1-based row number in the source file.")
    values: dict[str, str] = Field(description="Mapped values that failed validation.")
    error: str = Field(description="Why the row was rejected.")


class QuickCreateResponse(BaseModel):
    app: AppDetail
    imported: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    pii_columns: list[str] = Field(default_factory=list)
    blocked_pii: list[str] = Field(default_factory=list)
    failed_rows: list[FailedRowOut] = Field(default_factory=list)


class GameSessionBody(BaseModel):
    game_id: str = Field(description="Configured external game id.")


class GameSessionOut(BaseModel):
    session_id: str
    game_id: str
    name: str
    url: str = Field(description="External game entry URL.")
    token: str = Field(description="Short-lived token for the result endpoint.")
    expires_at: int = Field(description="Unix expiry for the token.")
    sandbox: str = Field(description="Iframe sandbox policy for this game.")


class RowMeta(BaseModel):
    row_id: str = Field(
        default="",
        description="Stable row id for update/delete; empty for legacy CSV apps.",
    )
    version: int = Field(default=1, description="Row version for optimistic concurrency.")
    source: str = Field(
        default="manual",
        description="How the row got here: manual, import, migration, connector, agent.",
    )


class AppRowsResponse(BaseModel):
    columns: list[AppColumn]
    rows: list[dict[str, str]]
    row_meta: list[RowMeta] = Field(
        default_factory=list,
        description="Parallel to rows: row id and version for editing.",
    )
    total: int
    limit: int
    offset: int
    table: str = Field(default="main", description="Table the rows came from.")
    pii_columns: list[str] = Field(
        default_factory=list,
        description="Sensitive columns whose values are masked for this caller.",
    )


class AppendRowBody(BaseModel):
    row: dict[str, Any] = Field(description="One row keyed by column name.")
    table: str = Field(default="main", description="Table to append to.")
    client_request_id: str | None = Field(
        default=None,
        max_length=200,
        description="Idempotency key; retrying with the same id does not duplicate the row.",
    )


class AppendRowResponse(BaseModel):
    row_index: int = Field(description="Zero-based index of the appended row.")
    row: dict[str, str]
    table: str = Field(default="main", description="Table the row was appended to.")
    row_id: str = Field(default="", description="Stable row id for later updates.")
    version: int = Field(default=1, description="Version of the appended row.")


class UpdateRowBody(BaseModel):
    row: dict[str, Any] = Field(description="Complete row keyed by column name.")
    expected_version: int | None = Field(
        default=None,
        description="Reject the update when the stored version differs.",
    )


class UpdateRowResponse(BaseModel):
    row_id: str
    row: dict[str, str]
    version: int


class DeleteRowResponse(BaseModel):
    deleted: str


class ColumnTypeSpec(BaseModel):
    type: str | None = Field(default=None, description="New column type.")
    label: str | None = Field(default=None, description="New display label.")
    required: bool | None = Field(default=None, description="Required flag.")
    values: list[str] | None = Field(default=None, description="Allowed values for an enum.")


class AlterSchemaBody(BaseModel):
    renames: dict[str, str] = Field(
        default_factory=dict,
        description="Old column name -> new column name.",
    )
    drops: list[str] = Field(
        default_factory=list,
        description="Columns to remove; their data is deleted.",
    )
    types: dict[str, ColumnTypeSpec] = Field(
        default_factory=dict,
        description="Column name -> new type/label/required/enum values.",
    )
    pii: dict[str, str | None] = Field(
        default_factory=dict,
        description=(
            "Column name -> sensitive-data kind (id_card, bank_card, phone, "
            "address, salary, email, secret). Pass null to clear."
        ),
    )


class UpdateAppBody(BaseModel):
    name: str | None = Field(default=None, description="New display name.")
    description: str | None = Field(default=None, description="New description.")


class CapabilitiesBody(BaseModel):
    capabilities: list[str] = Field(
        description="Page-facing capabilities to grant; an empty list denies all page actions."
    )


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


def _audit(
    server: OctopServer,
    *,
    actor: str,
    action: str,
    target: str,
    payload: dict[str, Any] | None = None,
) -> None:
    if server.services is None:
        return
    server.services.audit_repo.write(
        actor=actor,
        action=action,
        target=target,
        payload=json.dumps(payload, ensure_ascii=False) if payload else None,
    )


def _service(server: OctopServer) -> AppService:
    if server.services is None or server.app_runtime is None:
        raise OctopError(ErrorCode.INTERNAL_ERROR, "server is not started")
    return AppService(
        app_repo=server.services.light_app_repo,
        agent_repo=server.services.agent_repo,
        user_repo=server.services.user_repo,
        workspaces=server.app_runtime.agent_registry,
        secret_repo=server.services.secret_repo,
    )


def _columns(schema: Schema) -> list[AppColumn]:
    return [
        AppColumn(
            name=col.name,
            label=col.label,
            type=col.type,
            required=col.required,
            values=list(col.values),
            pii=col.pii,
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
        public_visible=table.public_visible,
        columns=columns,
    )


async def _table_outs(service: AppService, row: AppRecord) -> list[AppTableOut]:
    return [await _table_out(service, row, table) for table in service.list_tables(row)]


async def _app_detail(service: AppService, row: AppRecord) -> AppDetail:
    schema = await service.read_schema(row)
    base = _summary(service, row, "edit_structure")
    return AppDetail(
        **base.model_dump(exclude={"has_html", "has_preview"}),
        columns=_columns(schema),
        has_html=await service.has_html(row),
        has_preview=await service.has_preview(row),
        tables=await _table_outs(service, row),
    )


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


def _public_link(server: OctopServer, token: str) -> str:
    """Absolute public link when ``public_base_url`` is configured, else a relative path.

    The dashboard copies this value verbatim, so it must carry the public origin
    (scheme, host and port) the server is reached at — not the browser's.
    """
    base = (getattr(server.config, "public_base_url", "") or "").strip().rstrip("/")
    path = f"/p/{token}"
    return f"{base}{path}" if base else path


def _assistant(server: OctopServer, service: AppService) -> AppAssistant:
    if server.services is None or server.app_runtime is None:
        raise OctopError(ErrorCode.INTERNAL_ERROR, "server is not started")
    return AppAssistant(
        service=service,
        app_repo=server.services.light_app_repo,
        agents=server.app_runtime.agent_registry,
        usage_repo=server.services.usage_repo,
        policy_repo=server.services.user_policy_repo,
    )


def _generator(server: OctopServer) -> AppGenerator:
    if server.services is None or server.app_runtime is None:
        raise OctopError(ErrorCode.INTERNAL_ERROR, "server is not started")
    return AppGenerator(
        agents=server.app_runtime.agent_registry,
        usage_repo=server.services.usage_repo,
        policy_repo=server.services.user_policy_repo,
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
        secret_repo=server.services.secret_repo,
    )


def _require_thread_access(server: OctopServer, agent_id: str, thread_id: str, user: User) -> None:
    """The caller must own both the agent and the conversation."""
    if server.app_runtime is None:
        raise OctopError(ErrorCode.INTERNAL_ERROR, "server is not started")
    require_agent_row(agent_id, user=user, as_user=None, server=server)
    thread = server.app_runtime.gateway.thread_registry.get_thread(thread_id)
    if thread is None or thread.agent_id != agent_id:
        raise OctopError(ErrorCode.AGENT_NOT_FOUND, f"thread {thread_id!r} not found")
    if thread.user_id != user.id:
        raise OctopError(ErrorCode.FORBIDDEN, "thread not owned by user")


async def _thread_app_messages(server: OctopServer, agent_id: str, thread_id: str) -> list[Any]:
    """Every stored message that can carry an ``app_card`` in this thread."""
    if server.services is None or server.app_runtime is None:
        raise OctopError(ErrorCode.INTERNAL_ERROR, "server is not started")
    from octop.infra.history.service import HistoryArchive  # noqa: PLC0415

    archive = getattr(server.app_runtime, "history_archive", None)
    if isinstance(archive, HistoryArchive):
        from octop.infra.history.reader import export_messages  # noqa: PLC0415

        return await export_messages(
            archive, server.app_runtime.agent_registry, agent_id, thread_id
        )
    repo = server.services.thread_message_repo
    if await asyncio.to_thread(repo.projection_status, thread_id) != "ready":
        # History is still being projected; the dashboard keeps its transcript
        # snapshot until the next open instead of scanning the checkpoint here.
        return []
    rows = await asyncio.to_thread(repo.rows_containing, thread_id, "app_card")
    return [json.loads(row.message_json) for row in rows]


@router.get(
    "",
    response_model=AppListResponse,
    summary="List MiniApps",
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
    "/in-thread",
    response_model=AppListResponse,
    summary="List MiniApps in a conversation",
    description=(
        "Scans the whole thread transcript for ``app_card`` tool results and returns "
        "the MiniApps it referenced, most recently mentioned first. The chat dock's "
        "MiniApps tab needs this because it only keeps the latest history page."
    ),
)
async def list_in_thread_apps(
    agent_id: str = Query(description="Agent that owns the conversation."),
    thread_id: str = Query(description="Conversation thread id."),
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> AppListResponse:
    _require_thread_access(server, agent_id, thread_id, user)
    app_ids = app_card_ids(await _thread_app_messages(server, agent_id, thread_id))
    views = await _service(server).views_for_app_ids(app_ids, user.id, is_admin=user.is_admin)
    return AppListResponse(apps=[_summary_from_view(view) for view in views])


class AppTemplateOut(BaseModel):
    template: str = Field(description="Template id (legacy field).")
    template_id: str = Field(default="", description="Template id within its source.")
    label: str = Field(description="Display label.")
    name: str = Field(default="", description="Localized display name.")
    description: str = Field(default="", description="Localized one-line description.")
    category: str = Field(default="", description="Optional category (sales, finance, …).")
    version: int = Field(default=0, description="Template version.")
    has_html: bool = Field(description="Whether the template ships an app.html shell.")
    has_preview: bool = Field(
        default=False, description="Whether the template ships a preview image."
    )
    source: str = Field(default="agent", description="system or agent.")
    agent_id: str | None = Field(default=None, description="Owning agent for agent templates.")
    scope: str = Field(default="agent", description="global or agent.")


class AppTemplatesResponse(BaseModel):
    templates: list[AppTemplateOut]


class CreateFromTemplateBody(BaseModel):
    agent_id: str = Field(description="Agent whose workspace hosts the new app.")
    template: str = Field(description="Template id from the templates endpoint.")
    source: Literal["system", "agent"] = Field(
        default="agent", description="Template source: system (global) or agent (workspace)."
    )
    name: str | None = Field(default=None, max_length=100, description="New app name.")
    description: str = Field(default="", max_length=2000, description="New app description.")


class QuickInspectResponse(BaseModel):
    upload_path: str = Field(description="Workspace path to pass to quick/create.")
    sheet: str = Field(description="Sheet name inspected by default.")
    report: dict[str, Any] = Field(description="Deterministic inspection report.")
    suggested_view: dict[str, Any] = Field(description="Suggested dashboard view spec.")


class QuickSheetBody(BaseModel):
    sheet: str = Field(description="Sheet name from the inspection report.")
    columns: list[AppColumn] = Field(description="Confirmed fields for this sheet.")
    header_row: int | None = Field(
        default=None, ge=0, le=100, description="0-based header row override."
    )
    mapping: dict[str, str] | None = Field(
        default=None, description="Source header -> schema column map."
    )
    metric_column: str | None = Field(default=None, description="Numeric column for KPIs/charts.")
    group_column: str | None = Field(default=None, description="Categorical column for grouping.")
    view: Literal["dashboard", "ledger", "board"] = Field(
        default="dashboard", description="Generated view style for this sheet."
    )


class QuickCreateBody(BaseModel):
    agent_id: str = Field(description="Agent that will host the new app.")
    path: str = Field(description="Workspace path returned by quick/inspect.")
    columns: list[AppColumn] = Field(
        default_factory=list, description="Confirmed fields for a single-sheet import."
    )
    sheets: list[QuickSheetBody] | None = Field(
        default=None,
        description="Multi-sheet import; when present, top-level columns/sheet are ignored.",
    )
    view: Literal["dashboard", "ledger", "board"] = Field(
        default="dashboard", description="Generated view style."
    )
    name: str = Field(description="New app name.")
    description: str = Field(default="", max_length=2000, description="New app description.")
    sheet: str | None = Field(default=None, description="Sheet to import; defaults to first.")
    header_row: int | None = Field(
        default=None, ge=0, le=100, description="0-based header row, when overriding detection."
    )
    mapping: dict[str, str] | None = Field(
        default=None, description="Source header -> schema column map for quick imports."
    )
    metric_column: str | None = Field(
        default=None, description="Numeric column used for KPIs and charts."
    )
    group_column: str | None = Field(
        default=None, description="Categorical column used for the bar chart and board."
    )
    include_pii: bool = Field(default=False, description="Import columns detected as sensitive.")
    upsert_key: list[str] | None = Field(
        default=None, description="Columns whose combined value identifies an existing row."
    )


def _assert_agent_access(service: AppService, user: User, agent_id: str) -> None:
    if user.is_admin or service.is_agent_accessible(agent_id, user.id):
        return
    raise OctopError(ErrorCode.APP_FORBIDDEN, "agent is not accessible to this user")


@router.get(
    "/templates",
    response_model=AppTemplatesResponse,
    summary="List app templates",
    description=(
        "Lists system-wide templates plus the shells seeded into the agent's "
        "``app_templates/`` directory. Create one with POST /apps/from-template."
    ),
)
async def list_app_templates(
    request: Request,
    agent_id: str = Query(description="Agent whose workspace holds agent templates."),
    source: Literal["all", "system", "agent"] = Query(
        "agent", description="all: system + agent; system: bundled only; agent: workspace only."
    ),
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> AppTemplatesResponse:
    service = _service(server)
    _assert_agent_access(service, user, agent_id)
    templates = await service.list_templates(
        agent_id, source=source, locale=str(resolve_request_locale(request))
    )
    return AppTemplatesResponse(
        templates=[AppTemplateOut(**item) for item in templates],
    )


@router.get(
    "/templates/{template_id}/preview",
    summary="Fetch a template preview image",
    description=(
        "Serves the preview image (the SVG cover) of a system or agent "
        "template, so the creation gallery can show what the template looks "
        "like."
    ),
)
async def get_app_template_preview(
    template_id: str,
    agent_id: str = Query(description="Agent whose workspace holds agent templates."),
    source: Literal["system", "agent"] = Query(
        "agent", description="Template source: system (global) or agent (workspace)."
    ),
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> Response:
    service = _service(server)
    _assert_agent_access(service, user, agent_id)
    svg = await service.template_preview(agent_id, template_id, source)
    if svg is None:
        raise OctopError(ErrorCode.APP_NOT_FOUND, "template has no preview image")
    try:
        clean = validate_preview_svg(svg)
    except ValueError:
        raise OctopError(ErrorCode.APP_NOT_FOUND, "template has no preview image") from None
    return _svg_response(clean)


@router.post(
    "/from-template",
    response_model=AppDetail,
    status_code=201,
    summary="Create an app from an expert template",
    description=(
        "Creates an empty app from an expert-provided schema + HTML shell. "
        "The template never carries user data. Requires access to the agent."
    ),
)
async def create_app_from_template(
    body: CreateFromTemplateBody,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> AppDetail:
    service = _service(server)
    _assert_agent_access(service, user, body.agent_id)
    row = await service.create_from_template(
        agent_id=body.agent_id,
        owner_user_id=user.id,
        template=body.template,
        source=body.source,
        name=body.name,
        description=body.description,
    )
    return await _app_detail(service, row)


@router.post(
    "/quick/inspect",
    response_model=QuickInspectResponse,
    summary="Inspect a ledger for quick creation",
    description=(
        "Parses an uploaded spreadsheet, stores it under the agent's "
        "``inbound/imports/`` and returns inferred fields plus a dashboard "
        "preview. No app is created yet."
    ),
)
async def quick_inspect_app(
    file: UploadFile = File(...),  # noqa: B008
    agent_id: str = Form(description="Agent whose workspace stores the upload."),
    sheet: str | None = Form(default=None, description="Sheet name or 0-based index."),
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> QuickInspectResponse:
    service = _service(server)
    _assert_agent_access(service, user, agent_id)
    max_bytes = (
        int(server.services.config.max_upload_bytes) if server.services is not None else None
    )
    fallback = upload_mb_to_bytes(DEFAULT_MAX_UPLOAD_MB)
    data = await read_upload_capped(file, max_bytes=max_bytes or fallback)
    result = await service.quick_inspect(
        agent_id,
        filename=file.filename or "upload.xlsx",
        data=data,
        sheet=sheet,
    )
    return QuickInspectResponse(**result)


@router.post(
    "/quick/create",
    response_model=QuickCreateResponse,
    status_code=201,
    summary="Create a MiniApp from an inspected ledger",
    description=(
        "Creates the app, imports the uploaded rows and renders a deterministic "
        "dashboard, ledger or board. The app is deleted if rendering fails."
    ),
)
async def quick_create_app(
    body: QuickCreateBody,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> QuickCreateResponse:
    service = _service(server)
    _assert_agent_access(service, user, body.agent_id)
    if body.sheets:
        row, result = await service.quick_create_multi(
            agent_id=body.agent_id,
            owner_user_id=user.id,
            path=body.path,
            sheets=[sheet.model_dump(exclude_none=True) for sheet in body.sheets],
            name=body.name,
            description=body.description,
            include_pii=body.include_pii,
        )
        audit_payload = {"path": body.path, "sheets": [sheet.sheet for sheet in body.sheets]}
    else:
        if not body.columns:
            raise OctopError(ErrorCode.APP_ROW_INVALID, "at least one column is required")
        row, result = await service.quick_create(
            agent_id=body.agent_id,
            owner_user_id=user.id,
            path=body.path,
            columns=[column.model_dump(exclude_none=True) for column in body.columns],
            view=body.view,
            name=body.name,
            description=body.description,
            sheet=body.sheet,
            header_row=body.header_row,
            mapping=body.mapping,
            metric_column=body.metric_column,
            group_column=body.group_column,
            include_pii=body.include_pii,
            upsert_key=body.upsert_key,
        )
        audit_payload = {"path": body.path, "view": body.view}
    _audit(
        server,
        actor=f"user:{user.id}",
        action="miniapp.quick_create",
        target=row.app_id,
        payload=audit_payload,
    )
    return QuickCreateResponse(
        app=await _app_detail(service, row),
        imported=result.imported,
        updated=result.updated,
        skipped=result.skipped,
        errors=result.errors,
        warnings=result.warnings,
        pii_columns=result.pii_columns,
        blocked_pii=result.blocked_pii,
        failed_rows=[FailedRowOut(**item) for item in result.failed_rows],
    )


@router.post(
    "/{app_id}/game-sessions",
    response_model=GameSessionOut,
    status_code=201,
    summary="Create an external game session",
    description=(
        "Issues a short-lived token for a configured external game. The game "
        "opens in the platform side panel and writes results through "
        "POST /api/game-sessions/{token}/results."
    ),
)
async def create_app_game_session(
    app_id: str,
    body: GameSessionBody,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> GameSessionOut:
    service = _service(server)
    row = service.get(app_id)
    # Results are written into the app, so a game session needs write access.
    service.require(row, user.id, "edit_data", is_admin=user.is_admin)
    service.require_capability(row, app_capabilities.CAP_GAME_LAUNCH)
    if server.services is None:
        raise OctopError(ErrorCode.INTERNAL_ERROR, "server is not started")
    game = next(
        (item for item in server.services.config.games if item.id == body.game_id),
        None,
    )
    if game is None:
        raise OctopError(ErrorCode.APP_NOT_FOUND, f"unknown game: {body.game_id!r}")
    secret = server.services.secret_repo.get("jwt")
    if secret is None:
        raise OctopError(ErrorCode.INTERNAL_ERROR, "jwt secret missing")
    session_id = f"game_{new_short_id(16)}"
    token, expires_at = create_game_token(
        secret,
        app_id=row.app_id,
        user_id=user.id,
        game_id=game.id,
        session_id=session_id,
        is_admin=user.is_admin,
    )
    _audit(
        server,
        actor=f"user:{user.id}",
        action="miniapp.game_session",
        target=row.app_id,
        payload={"game_id": game.id, "session_id": session_id},
    )
    return GameSessionOut(
        session_id=session_id,
        game_id=game.id,
        name=game.name,
        url=game.url,
        token=token,
        expires_at=expires_at,
        sandbox=game.sandbox,
    )


@router.post(
    "/restore",
    response_model=AppDetail,
    status_code=201,
    summary="Restore an app from a backup",
    description=(
        "Creates a new app from a zip produced by GET /apps/{app_id}/backup. "
        "The archive is validated before anything is written; ids are not "
        "reused. Requires access to the target agent."
    ),
)
async def restore_app(
    file: UploadFile = File(...),  # noqa: B008
    agent_id: str = Query(description="Agent whose workspace will host the restored app."),
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> AppDetail:
    service = _service(server)
    _assert_agent_access(service, user, agent_id)
    data = await read_upload_capped(
        file,
        max_bytes=MAX_BACKUP_BYTES,
        code=ErrorCode.APP_SOURCE_INVALID,
    )
    row = await service.restore_archive(
        agent_id=agent_id,
        owner_user_id=user.id,
        data=data,
    )
    _audit(
        server,
        actor=f"user:{user.id}",
        action="light_app.restore",
        target=row.app_id,
        payload={"bytes": len(data)},
    )
    schema = await service.read_schema(row)
    base = _summary(service, row, "edit_structure")
    return AppDetail(
        **base.model_dump(exclude={"has_html", "has_preview"}),
        columns=_columns(schema),
        has_html=await service.has_html(row),
        has_preview=await service.has_preview(row),
        tables=await _table_outs(service, row),
    )


@router.get(
    "/{app_id}",
    response_model=AppDetail,
    summary="Get MiniApp details",
)
async def get_app(
    app_id: str,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> AppDetail:
    from octop.infra.metrics import METRICS  # noqa: PLC0415

    METRICS.inc("light_app_open_total")
    service, row, permission = _row_access(server, user, app_id, "view")
    schema = await service.read_schema(row)
    base = _summary(service, row, permission)
    return AppDetail(
        # The detail view checks the workspace directly: the cached size is a
        # hint for list views, the file itself is the truth.
        **base.model_dump(exclude={"has_html", "has_preview"}),
        columns=_columns(schema),
        has_html=await service.has_html(row),
        has_preview=await service.has_preview(row),
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


@router.patch(
    "/{app_id}/tables/{table_key}",
    response_model=AppTableOut,
    summary="Set table visibility",
    description=(
        "Expose or hide one table on the app's public read-only link. Requires edit_structure."
    ),
)
async def set_app_table_visibility(
    app_id: str,
    table_key: str,
    body: TableVisibilityBody,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> AppTableOut:
    service, row, _permission = _row_access(server, user, app_id, "edit_structure")
    view = await service.set_table_visibility(
        row,
        table=table_key,
        public_visible=body.public_visible,
    )
    _audit(
        server,
        actor=f"user:{user.id}",
        action="light_app.table.visibility",
        target=app_id,
        payload={"table": table_key, "public_visible": body.public_visible},
    )
    return await _table_out(service, row, view)


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


class AssetOut(BaseModel):
    name: str = Field(description="Asset file name inside the app's assets directory.")
    size: int = Field(default=0, description="Asset size in bytes.")


class AssetListResponse(BaseModel):
    assets: list[AssetOut]


@router.get(
    "/{app_id}/assets",
    response_model=AssetListResponse,
    summary="List app assets",
    description="Lists the images stored next to the app (covers, icons).",
)
async def list_app_assets(
    app_id: str,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> AssetListResponse:
    service, row, _permission = _row_access(server, user, app_id, "view")
    names = await service.list_assets(row)
    return AssetListResponse(assets=[AssetOut(name=name) for name in names])


@router.post(
    "/{app_id}/assets",
    response_model=AssetOut,
    status_code=201,
    summary="Upload an app asset",
    description=(
        "Stores a PNG, JPEG, WebP or GIF image (at most 5 MB) next to the app. "
        "The app page cannot load remote images, so covers and icons are "
        "stored here. Requires edit_data."
    ),
)
async def upload_app_asset(
    app_id: str,
    file: UploadFile = File(...),  # noqa: B008
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> AssetOut:
    service, row, _permission = _row_access(server, user, app_id, "edit_data")
    data = await read_upload_capped(
        file,
        max_bytes=MAX_ASSET_BYTES,
        code=ErrorCode.APP_SOURCE_INVALID,
    )
    name = await service.save_asset(row, file.filename or "asset.png", data)
    return AssetOut(name=name, size=len(data))


@router.get(
    "/{app_id}/assets/{name}",
    summary="Fetch an app asset",
    description=(
        "Serves one stored image. The app page fetches it through this "
        "endpoint, so the app ACL applies. Requires view."
    ),
)
async def get_app_asset(
    app_id: str,
    name: str,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> Response:
    service, row, _permission = _row_access(server, user, app_id, "view")
    mime, data = await service.read_asset(row, name)
    return Response(
        content=data,
        media_type=mime,
        headers={"Cache-Control": "private, max-age=300"},
    )


def _svg_response(svg: str) -> Response:
    """Serve agent-drawn SVG markup as an image.

    The markup is validated on write; the sandboxing CSP is the second layer
    that keeps a preview inert when it is opened directly.
    """
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={
            "Content-Security-Policy": (
                "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:"
            ),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=300",
        },
    )


@router.get(
    "/{app_id}/preview",
    summary="Fetch an app preview image",
    description=(
        "Serves the app's preview image: the SVG cover the agent draws for "
        "the app list. Requires view."
    ),
)
async def get_app_preview(
    app_id: str,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> Response:
    service, row, _permission = _row_access(server, user, app_id, "view")
    svg = await service.read_preview(row)
    if svg is None:
        raise OctopError(ErrorCode.APP_NOT_FOUND, "app has no preview image")
    try:
        clean = validate_preview_svg(svg)
    except ValueError:
        # The file may have been written straight into the workspace, so the
        # markup is checked again before it is served.
        raise OctopError(ErrorCode.APP_NOT_FOUND, "app has no preview image") from None
    return _svg_response(clean)


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
    service, row, permission = _row_access(server, user, app_id, "view")
    redact_pii = permission == "view"
    schema, rows, row_meta, total = await service.read_rows_detailed(
        row,
        limit=limit,
        offset=offset,
        table=table,
        redact_pii=redact_pii,
    )
    return AppRowsResponse(
        columns=_columns(schema),
        rows=rows,
        row_meta=[RowMeta(**meta) for meta in row_meta],
        total=total,
        limit=limit,
        offset=offset,
        table=table,
        pii_columns=[column.name for column in schema.columns if column.pii] if redact_pii else [],
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
    index, stored = await service.append_row_record(
        row,
        body.row,
        table=body.table,
        client_request_id=body.client_request_id,
        created_by=user.id,
    )
    from octop.infra.metrics import METRICS  # noqa: PLC0415

    METRICS.inc("light_app_row_write_total")
    return AppendRowResponse(
        row_index=index,
        row=stored.values,
        table=body.table,
        row_id=stored.row_id,
        version=stored.version,
    )


@router.patch(
    "/{app_id}/rows/{row_id}",
    response_model=UpdateRowResponse,
    summary="Update a row",
    description=(
        "Replaces one row. Pass expected_version to reject a stale edit with 409. "
        "Requires edit_data."
    ),
)
async def update_app_row(
    app_id: str,
    row_id: str,
    body: UpdateRowBody,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> UpdateRowResponse:
    service, row, _permission = _row_access(server, user, app_id, "edit_data")
    result = await service.update_row(
        row,
        row_id,
        body.row,
        expected_version=body.expected_version,
    )
    from octop.infra.metrics import METRICS  # noqa: PLC0415

    METRICS.inc("light_app_row_write_total")
    return UpdateRowResponse(
        row_id=str(result["row_id"]),
        row=result["values"],
        version=int(result["version"]),
    )


@router.delete(
    "/{app_id}/rows/{row_id}",
    response_model=DeleteRowResponse,
    summary="Delete a row",
    description="Deletes one row. Pass expected_version to reject a stale edit. Requires edit_data.",
)
async def delete_app_row(
    app_id: str,
    row_id: str,
    expected_version: int | None = Query(default=None, description="Reject when the row changed."),
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> DeleteRowResponse:
    service, row, _permission = _row_access(server, user, app_id, "edit_data")
    deleted = await service.delete_row(row, row_id, expected_version=expected_version)
    from octop.infra.metrics import METRICS  # noqa: PLC0415

    METRICS.inc("light_app_row_write_total")
    return DeleteRowResponse(deleted=deleted)


@router.get(
    "/{app_id}/export",
    summary="Export a table as CSV",
    description=(
        "Renders the current rows of one table as a CSV snapshot. Requires view. "
        "This is an export, not the live storage."
    ),
)
async def export_app_table(
    app_id: str,
    table: str = Query(default=MAIN_TABLE_KEY, description="Table key to export."),
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> Response:
    service, row, permission = _row_access(server, user, app_id, "view")
    redact_pii = permission == "view"
    filename, content = await service.export_rows(
        row,
        table=table,
        redact_pii=redact_pii,
    )
    from octop.infra.metrics import METRICS  # noqa: PLC0415

    METRICS.inc("light_app_export_total")
    _audit(
        server,
        actor=f"user:{user.id}",
        action="light_app.export",
        target=app_id,
        payload={"table": table, "bytes": len(content), "pii_masked": redact_pii},
    )
    return Response(
        content=content.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/{app_id}/backup",
    summary="Download an app backup",
    description=(
        "Bundles app.html, every table's schema and rows, and the assets into "
        "one zip. Restore it with POST /apps/restore. Requires edit_structure "
        "because the archive contains unmasked data."
    ),
    responses={200: {"content": {"application/zip": {}}}},
)
async def backup_app(
    app_id: str,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> Response:
    service, row, _permission = _row_access(server, user, app_id, "edit_structure")
    filename, content = await service.backup_archive(row)
    from octop.infra.metrics import METRICS  # noqa: PLC0415

    METRICS.inc("light_app_export_total")
    _audit(
        server,
        actor=f"user:{user.id}",
        action="light_app.backup",
        target=app_id,
        payload={"bytes": len(content)},
    )
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.patch(
    "/{app_id}/schema",
    response_model=AppDetail,
    summary="Rename, drop or retype columns",
    description=(
        "Migrates every existing row to the new schema in one transaction. The "
        "change is rejected when a row cannot be represented (for example a "
        "non-numeric value when changing a column to integer). Requires "
        "edit_structure."
    ),
)
async def alter_app_schema(
    app_id: str,
    body: AlterSchemaBody,
    table: str = Query(default=MAIN_TABLE_KEY, description="Table key to alter."),
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> AppDetail:
    service, row, permission = _row_access(server, user, app_id, "edit_structure")
    await service.alter_schema(
        row,
        table=table,
        renames=body.renames,
        drops=body.drops,
        types={name: spec.model_dump(exclude_none=True) for name, spec in body.types.items()},
        pii=body.pii,
    )
    updated = service.get(app_id)
    schema = await service.read_schema(updated, table)
    base = _summary(service, updated, permission)
    return AppDetail(
        **base.model_dump(exclude={"has_html", "has_preview"}),
        columns=_columns(schema),
        has_html=await service.has_html(updated),
        has_preview=await service.has_preview(updated),
        tables=await _table_outs(service, updated),
    )


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
        **base.model_dump(exclude={"has_html", "has_preview"}),
        columns=_columns(schema),
        has_html=await service.has_html(row),
        has_preview=await service.has_preview(row),
        tables=await _table_outs(service, row),
    )


@router.put(
    "/{app_id}/capabilities",
    response_model=AppDetail,
    summary="Update MiniApp page capabilities",
    description=(
        "Replaces the capability manifest that gates what the sandboxed app page may "
        "ask the host to do. This does not restrict the agent tools or the dashboard's "
        "own data panel. Requires edit_structure."
    ),
)
async def update_app_capabilities(
    app_id: str,
    body: CapabilitiesBody,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> AppDetail:
    service, row, _permission = _row_access(server, user, app_id, "edit_structure")
    row = service.set_capabilities(row, body.capabilities)
    _audit(
        server,
        actor=f"user:{user.id}",
        action="light_app.capabilities.update",
        target=app_id,
        payload={"capabilities": service.capability_list(row)},
    )
    return await _app_detail(service, row)


@router.delete(
    "/{app_id}",
    response_model=DeleteAppResponse,
    summary="Delete a MiniApp",
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
    _audit(
        server,
        actor=f"user:{user.id}",
        action="light_app.share",
        target=app_id,
        payload={"shares": [share.model_dump() for share in body.shares]},
    )
    return SharesResponse(shares=[ShareOut(**asdict(view)) for view in views])


class PublicationOut(BaseModel):
    active: bool = Field(description="Whether a non-revoked, non-expired link exists.")
    url: str | None = Field(default=None, description="Public URL when a token was just created.")
    token: str | None = Field(
        default=None, description="Plaintext token, returned only on creation."
    )
    expires_at: int | None = Field(default=None, description="Unix expiry, or null for no expiry.")


class CreatePublicationBody(BaseModel):
    ttl_days: int = Field(
        default=0,
        ge=0,
        le=3650,
        description="Link lifetime in days; 0 means no expiry.",
    )


@router.get(
    "/{app_id}/publication",
    response_model=PublicationOut,
    summary="Get the public link status",
    description="Read-only public link status. Requires view.",
)
async def get_app_publication(
    app_id: str,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> PublicationOut:
    service, row, _permission = _row_access(server, user, app_id, "view")
    publication = service.get_publication(row)
    active = (
        publication is not None
        and publication.revoked_at is None
        and (publication.expires_at is None or publication.expires_at > int(time.time()))
    )
    return PublicationOut(
        active=active,
        expires_at=publication.expires_at if publication else None,
    )


@router.post(
    "/{app_id}/publication",
    response_model=PublicationOut,
    summary="Create or rotate the public link",
    description=(
        "Creates a read-only public link and returns the token once. "
        "Rotating the link invalidates the previous one. Requires edit_structure."
    ),
)
async def create_app_publication(
    app_id: str,
    body: CreatePublicationBody,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> PublicationOut:
    service, row, _permission = _row_access(server, user, app_id, "edit_structure")
    token, expires_at = service.create_publication(
        row,
        user_id=user.id,
        ttl_days=body.ttl_days,
    )
    _audit(
        server,
        actor=f"user:{user.id}",
        action="light_app.publication.create",
        target=app_id,
        payload={"expires_at": expires_at},
    )
    return PublicationOut(
        active=True,
        url=_public_link(server, token),
        token=token,
        expires_at=expires_at,
    )


@router.delete(
    "/{app_id}/publication",
    response_model=PublicationOut,
    summary="Revoke the public link",
    description="Revokes the current link; an already shared URL returns 404. Requires edit_structure.",
)
async def revoke_app_publication(
    app_id: str,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> PublicationOut:
    service, row, _permission = _row_access(server, user, app_id, "edit_structure")
    service.revoke_publication(row)
    _audit(
        server,
        actor=f"user:{user.id}",
        action="light_app.publication.revoke",
        target=app_id,
    )
    return PublicationOut(active=False)


class TransferAppBody(BaseModel):
    agent_id: str = Field(
        min_length=1,
        description="The agent that should host the app from now on.",
    )


@router.post(
    "/{app_id}/transfer",
    response_model=AppDetail,
    summary="Move an app to another agent",
    description=(
        "Copies the app files into another agent's workspace and moves the "
        "app there. Use it to hand an app over to a different expert. "
        "Requires edit_structure."
    ),
)
async def transfer_app(
    app_id: str,
    body: TransferAppBody,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> AppDetail:
    service, row, _permission = _row_access(server, user, app_id, "edit_structure")
    moved = await service.transfer(row, target_agent_id=body.agent_id, actor_user_id=user.id)
    schema = await service.read_schema(moved)
    base = _summary(service, moved, "edit_structure")
    return AppDetail(
        **base.model_dump(exclude={"has_html", "has_preview"}),
        columns=_columns(schema),
        has_html=await service.has_html(moved),
        has_preview=await service.has_preview(moved),
        tables=await _table_outs(service, moved),
    )


class AskAssistantBody(BaseModel):
    question: str = Field(
        min_length=1,
        max_length=4000,
        description="The question about this app's data.",
    )


class AssistantChartSeriesOut(BaseModel):
    key: str = Field(description="Data key inside each chart point.")
    label: str = Field(description="Human-readable series name.")


class AssistantChartOut(BaseModel):
    type: Literal["bar", "line", "pie", "area", "scatter", "stacked_bar"] = Field(
        description="Chart type the dashboard should render."
    )
    title: str = Field(description="Chart title.")
    x: str = Field(description="Column used for the x axis.")
    x_label: str = Field(default="", description="Human-readable x column label.")
    series: list[AssistantChartSeriesOut] = Field(description="One entry per numeric series.")
    data: list[dict[str, Any]] = Field(
        description="Chart points; each has `x` plus one key per series."
    )
    sampled: bool = Field(
        default=False,
        description="True when the chart is based on the first 500 rows only.",
    )
    sample_size: int = Field(default=0, description="Rows used to build the chart.")
    matched_rows: int = Field(default=0, description="Rows matching the chart filters.")
    truncated: bool = Field(
        default=False,
        description="True when the series was capped to the requested point limit.",
    )


class AssistantMessageOut(BaseModel):
    role: str = Field(description="user or assistant.")
    content: str
    created_at: int
    charts: list[AssistantChartOut] = Field(
        default_factory=list,
        description="Chart specs attached to an assistant answer, if any.",
    )


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
            AssistantMessageOut(
                role=t.role,
                content=t.content,
                created_at=t.created_at,
                charts=[AssistantChartOut.model_validate(chart) for chart in t.charts],
            )
            for t in turns
        ],
    )


@router.post(
    "/{app_id}/assistant",
    summary="Ask the app assistant (SSE)",
    description=(
        "Streams SSE frames (event: chunk) whose data carries "
        "{type: token|tool|chart|done|error}. Read-only over the app's data; "
        "consumes the owning agent's model quota. Requires the view permission."
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
    from octop.infra.metrics import METRICS  # noqa: PLC0415

    METRICS.inc("light_app_assistant_total")
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


class GenerateBody(BaseModel):
    prompt: str = Field(
        min_length=1,
        max_length=8000,
        description="The instruction or text the model should process.",
    )
    system: str = Field(
        default="",
        max_length=4000,
        description="Optional extra system instruction supplied by the app.",
    )


class GenerateResponse(BaseModel):
    text: str = Field(description="Generated text, truncated to 16k characters.")
    model: str = Field(description="Model reference that answered.")
    input_tokens: int = Field(description="Prompt tokens billed to the owning agent.")
    output_tokens: int = Field(description="Completion tokens billed to the owning agent.")


@router.post(
    "/{app_id}/generate",
    response_model=GenerateResponse,
    summary="Generate text with the app's AI model",
    description=(
        "One-shot, non-streaming text generation using the owning agent's "
        "default model. App HTML calls it through `octopApp.generate(prompt, "
        "{system})`; the iframe never sees a JWT. Requires the view permission "
        "and consumes the owning agent's model quota. Public share links do not "
        "expose this endpoint."
    ),
)
async def generate_app_text(
    app_id: str,
    body: GenerateBody,
    request: Request,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> GenerateResponse:
    service, row, _permission = _row_access(server, user, app_id, "view")
    service.require_capability(row, app_capabilities.CAP_AI_GENERATE)
    prompt = body.prompt.strip()
    if not prompt:
        raise OctopError(ErrorCode.APP_PROMPT_INVALID, "prompt must not be empty")
    from octop.infra.metrics import METRICS  # noqa: PLC0415

    METRICS.inc("light_app_generate_total")
    locale = resolve_request_locale(request)
    generated = await _generator(server).generate(
        row,
        user_id=user.id,
        prompt=prompt,
        system=body.system,
        locale=locale,
    )
    # 成本归创建者，调用者身份单独留痕（可追溯，不参与计费归属）。
    assert server.services is not None
    server.services.audit_repo.write(
        actor=user.username,
        action="app.generate",
        target=row.app_id,
        payload=json.dumps(
            {
                "owner_agent_id": row.agent_id,
                "owner_user_id": row.owner_user_id,
                "caller_user_id": user.id,
                "model": generated.model_ref,
                "input_tokens": generated.input_tokens,
                "output_tokens": generated.output_tokens,
            },
            ensure_ascii=False,
        ),
    )
    return GenerateResponse(
        text=generated.text,
        model=generated.model_ref,
        input_tokens=generated.input_tokens,
        output_tokens=generated.output_tokens,
    )


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
    path: str = Field(
        default="",
        description="Workspace-relative path of the file to import.",
    )
    paths: list[str] = Field(
        default_factory=list,
        description=(
            "Import several workspace files into the same table, in order. "
            "Use either path or paths."
        ),
    )
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
    sheet: str | None = Field(
        default=None,
        description=(
            "Worksheet to import from: a sheet name or a 0-based index. "
            "Defaults to the first sheet."
        ),
    )
    header_row: int | None = Field(
        default=None,
        ge=0,
        le=100,
        description=(
            "0-based index of the header line, for files that start with a "
            "title or a note. Defaults to the first line."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description="Validate and count the rows without writing them.",
    )
    upsert_key: list[str] | None = Field(
        default=None,
        description=(
            "Columns whose combined value identifies an existing row. Rows with "
            "a matching key are updated instead of duplicated."
        ),
    )
    include_pii: bool = Field(
        default=False,
        description=(
            "Import columns detected as sensitive. When false, their values are "
            "dropped and only the rest of the row is written."
        ),
    )


class ImportResponse(BaseModel):
    table: str
    imported: int = Field(description="Rows written into the table.")
    skipped: int = Field(description="Rows dropped because they failed validation.")
    errors: list[str] = Field(description="Up to 5 row-level validation failures.")
    updated: int = Field(default=0, description="Rows replaced in place by upsert_key.")
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-fatal notes, e.g. columns that look like sensitive data.",
    )
    would_import: int = Field(
        default=0,
        description="Rows a dry run would have written; 0 for a real import.",
    )
    dry_run: bool = Field(default=False, description="True when nothing was written.")
    pii_columns: list[str] = Field(
        default_factory=list,
        description="Columns detected or marked as sensitive during this import.",
    )
    blocked_pii: list[str] = Field(
        default_factory=list,
        description="Sensitive columns skipped because include_pii was false.",
    )
    failed_rows: list[FailedRowOut] = Field(
        default_factory=list,
        description="Rejected rows with their errors, for correcting and retrying.",
    )


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
    if body.paths and body.path:
        raise OctopError(ErrorCode.APP_ROW_INVALID, "use either path or paths, not both")
    if not body.paths and not body.path:
        raise OctopError(ErrorCode.APP_ROW_INVALID, "an import path is required")
    if body.paths:
        result: ImportResult = await service.import_from_files(
            row,
            paths=body.paths,
            table=table_key,
            mode=body.mode,
            mapping=body.mapping,
            sheet=body.sheet,
            header_row=body.header_row,
            dry_run=body.dry_run,
            upsert_key=body.upsert_key,
            include_pii=body.include_pii,
        )
    else:
        result = await service.import_from_file(
            row,
            path=body.path,
            table=table_key,
            mode=body.mode,
            mapping=body.mapping,
            sheet=body.sheet,
            header_row=body.header_row,
            dry_run=body.dry_run,
            upsert_key=body.upsert_key,
            include_pii=body.include_pii,
        )
    from octop.infra.metrics import METRICS  # noqa: PLC0415

    METRICS.inc("light_app_row_write_total", result.imported + result.updated)
    return ImportResponse(
        table=table_key,
        imported=result.imported,
        skipped=result.skipped,
        errors=result.errors,
        updated=result.updated,
        warnings=result.warnings,
        would_import=result.would_import,
        dry_run=result.dry_run,
        pii_columns=result.pii_columns,
        blocked_pii=result.blocked_pii,
        failed_rows=[FailedRowOut(**item) for item in result.failed_rows],
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
    warnings: list[str] = Field(default_factory=list)
    pii_columns: list[str] = Field(default_factory=list)
    blocked_pii: list[str] = Field(default_factory=list)
    failed_rows: list[FailedRowOut] = Field(default_factory=list)


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
        warnings=result.warnings,
        pii_columns=result.pii_columns,
        blocked_pii=result.blocked_pii,
        failed_rows=[FailedRowOut(**item) for item in result.failed_rows],
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
