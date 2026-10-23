"""HTTP API for agent-built light apps.

Apps are created and edited by agents through built-in tools; this router only
exposes them so the dashboard and collaborators can render the UI and append
rows. Ownership and sharing are enforced through ``AppService``.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

from octop.api.deps import current_user, get_server
from octop.infra.apps.models import Schema
from octop.infra.apps.service import AppRecord, AppService
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.server import OctopServer
from octop.infra.users.identity import User

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
    row_count: int
    created_at: int
    updated_at: int


class AppListResponse(BaseModel):
    apps: list[AppSummary]


class AppDetail(AppSummary):
    columns: list[AppColumn] = Field(description="Schema columns in display order.")
    has_html: bool = Field(description="Whether the agent has written app.html yet.")


class AppRowsResponse(BaseModel):
    columns: list[AppColumn]
    rows: list[dict[str, str]]
    total: int
    limit: int
    offset: int


class AppendRowBody(BaseModel):
    row: dict[str, Any] = Field(description="One row keyed by column name.")


class AppendRowResponse(BaseModel):
    row_index: int = Field(description="Zero-based index of the appended row.")
    row: dict[str, str]


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
    return AppSummary(**asdict(service.view(row, permission)))


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
            AppSummary(**asdict(view))
            for view in service.list_views(user.id, is_admin=user.is_admin)
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
        **base.model_dump(),
        columns=_columns(schema),
        has_html=await service.has_html(row),
    )


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
    "/{app_id}/rows",
    response_model=AppRowsResponse,
    summary="Read app rows",
)
async def get_app_rows(
    app_id: str,
    limit: int = Query(200, ge=1, le=500, description="Rows to return."),
    offset: int = Query(0, ge=0, description="Rows to skip."),
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> AppRowsResponse:
    service, row, _permission = _row_access(server, user, app_id, "view")
    schema, rows, total = await service.read_rows(row, limit=limit, offset=offset)
    return AppRowsResponse(
        columns=_columns(schema),
        rows=rows,
        total=total,
        limit=limit,
        offset=offset,
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
    index, stored = await service.append_row(row, body.row)
    return AppendRowResponse(row_index=index, row=stored)


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
        **base.model_dump(),
        columns=_columns(schema),
        has_html=await service.has_html(row),
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
