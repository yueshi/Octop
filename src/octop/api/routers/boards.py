"""HTTP API for personal workbench boards."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from octop.api.deps import current_user, get_server
from octop.infra.apps.boards import BoardService, BoardView
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.server import OctopServer
from octop.infra.users.identity import User

router = APIRouter(prefix="/boards")


class BoardItemIn(BaseModel):
    app_id: str = Field(description="MiniApp id to place on the board.")
    x: int = Field(ge=0, description="Zero-based column in the 12-column grid.")
    y: int = Field(ge=0, description="Zero-based row; grows downward.")
    w: int = Field(ge=1, le=12, description="Width in grid columns.")
    h: int = Field(ge=1, le=12, description="Height in grid rows.")
    locked: bool = Field(
        default=False,
        description="Keep this cell out of auto-arrange and disable drag/resize.",
    )


class BoardLayoutOut(BaseModel):
    version: int = Field(description="Layout schema version (currently 1).")
    items: list[BoardItemIn] = Field(description="Cells sorted by row, then column.")


class BoardSummaryOut(BaseModel):
    board_id: str
    name: str
    description: str
    version: int = Field(description="CAS version; send it back when saving a layout.")
    item_count: int = Field(description="MiniApps currently placed on the board.")
    created_at: int
    updated_at: int


class BoardOut(BoardSummaryOut):
    layout: BoardLayoutOut = Field(
        description="Frozen board layout: {version, items: [{app_id, x, y, w, h}]}.",
    )


class CreateBoardBody(BaseModel):
    name: str = Field(min_length=1, max_length=100, description="Board display name.")
    description: str = Field(default="", max_length=500, description="Optional summary.")


class UpdateBoardBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)


class LayoutBody(BaseModel):
    version: int = Field(ge=1, description="Version returned by the last read/save.")
    items: list[BoardItemIn] = Field(
        default_factory=list,
        description="Complete replacement layout; at most 24 MiniApps.",
    )


class DeleteBoardResponse(BaseModel):
    deleted: str


def _service(server: OctopServer) -> BoardService:
    if server.services is None:
        raise OctopError(ErrorCode.INTERNAL_ERROR, "server is not started")
    return BoardService(
        board_repo=server.services.light_app_board_repo,
        app_repo=server.services.light_app_repo,
    )


def _summary(view: BoardView) -> BoardSummaryOut:
    return BoardSummaryOut(
        board_id=view.board_id,
        name=view.name,
        description=view.description,
        version=view.version,
        item_count=view.item_count,
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


def _out(view: BoardView) -> BoardOut:
    base = _summary(view)
    return BoardOut(
        **base.model_dump(),
        layout=BoardLayoutOut.model_validate(view.layout),
    )


@router.get("", response_model=list[BoardSummaryOut], summary="List workbench boards")
async def list_boards(
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> list[BoardSummaryOut]:
    """Boards owned by the caller, newest edit first."""
    return [_summary(view) for view in _service(server).list_boards(user.id)]


@router.post(
    "",
    response_model=BoardOut,
    status_code=201,
    summary="Create a workbench board",
)
async def create_board(
    body: CreateBoardBody,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> BoardOut:
    return _out(
        _service(server).create(user_id=user.id, name=body.name, description=body.description)
    )


@router.get("/{board_id}", response_model=BoardOut, summary="Get a workbench board")
async def get_board(
    board_id: str,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> BoardOut:
    return _out(
        _service(server).get(
            board_id,
            user_id=user.id,
            is_admin=user.is_admin,
        )
    )


@router.patch("/{board_id}", response_model=BoardOut, summary="Rename a workbench board")
async def update_board(
    board_id: str,
    body: UpdateBoardBody,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> BoardOut:
    return _out(
        _service(server).update(
            board_id,
            user_id=user.id,
            is_admin=user.is_admin,
            name=body.name,
            description=body.description,
        )
    )


@router.delete(
    "/{board_id}",
    response_model=DeleteBoardResponse,
    summary="Delete a workbench board",
)
async def delete_board(
    board_id: str,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> DeleteBoardResponse:
    _service(server).delete(board_id, user_id=user.id, is_admin=user.is_admin)
    return DeleteBoardResponse(deleted=board_id)


@router.put(
    "/{board_id}/layout",
    response_model=BoardOut,
    summary="Replace a board layout",
    description=(
        "Replaces every cell on the board. Send the version from the last read; "
        "a stale version returns 409 so two tabs do not overwrite each other."
    ),
)
async def set_board_layout(
    board_id: str,
    body: LayoutBody,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> BoardOut:
    return _out(
        _service(server).set_layout(
            board_id,
            user_id=user.id,
            is_admin=user.is_admin,
            items=[item.model_dump() for item in body.items],
            expected_version=body.version,
        )
    )
