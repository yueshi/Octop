"""Personal workbench boards: named layouts of MiniApps.

A board stores only app ids and grid rectangles; app content and access stay
in the MiniApp tables. Layout writes are CAS-guarded so two browser tabs do
not silently overwrite each other.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from octop.infra.db.repos.apps import LightAppRepo
from octop.infra.db.repos.boards import LightAppBoardRepo, LightAppBoardRow
from octop.infra.errors import ErrorCode, OctopError

GRID_COLS = 12
MAX_BOARD_ITEMS = 24
MAX_NAME_CHARS = 100
MAX_DESCRIPTION_CHARS = 500
DEFAULT_ITEM_W = 4
DEFAULT_ITEM_H = 4
LAYOUT_VERSION = 1


def _invalid(reason: str, **details: object) -> OctopError:
    return OctopError(ErrorCode.BOARD_LAYOUT_INVALID, reason, details=details)


def _clean_name(raw: object) -> str:
    value = str(raw or "").strip()
    if not value or len(value) > MAX_NAME_CHARS:
        raise OctopError(
            ErrorCode.BOARD_NAME_INVALID,
            f"board name must be 1-{MAX_NAME_CHARS} characters",
        )
    return value


def _clean_description(raw: object) -> str:
    return str(raw or "").strip()[:MAX_DESCRIPTION_CHARS]


def _int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _item(value: object, *, strict: bool) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        if strict:
            raise _invalid("each board item must be an object")
        return None
    app_id = value.get("app_id")
    x, y, w, h = (_int(value.get(key)) for key in ("x", "y", "w", "h"))
    if (
        not isinstance(app_id, str)
        or not app_id.strip()
        or x is None
        or y is None
        or w is None
        or h is None
        or x < 0
        or y < 0
        or w < 1
        or h < 1
        or w > GRID_COLS
        or h > GRID_COLS
        or x + w > GRID_COLS
    ):
        if strict:
            raise _invalid("invalid board item rectangle", item=value)
        return None
    locked_raw = value.get("locked", False)
    if not isinstance(locked_raw, bool):
        if strict:
            raise _invalid("board item locked must be a boolean", item=value)
        locked_raw = False
    item: dict[str, Any] = {
        "app_id": app_id.strip(),
        "x": x,
        "y": y,
        "w": w,
        "h": h,
    }
    if locked_raw:
        item["locked"] = True
    return item


def _sorted(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda item: (item["y"], item["x"], item["app_id"]))


def parse_layout(raw: str | None) -> dict[str, Any]:
    """Read stored JSON, dropping invalid rows instead of failing the page."""
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {"version": LAYOUT_VERSION, "items": []}
    raw_items = data.get("items") if isinstance(data, dict) else None
    items: list[dict[str, Any]] = []
    if isinstance(raw_items, list):
        for value in raw_items:
            item = _item(value, strict=False)
            if item is not None:
                items.append(item)
    return {"version": LAYOUT_VERSION, "items": _sorted(items)}


def validate_layout(raw_items: object) -> dict[str, Any]:
    """Validate a layout write; rejects bad rectangles and duplicate apps."""
    if not isinstance(raw_items, list):
        raise _invalid("layout items must be a list")
    if len(raw_items) > MAX_BOARD_ITEMS:
        raise _invalid(
            f"a board can contain at most {MAX_BOARD_ITEMS} MiniApps",
            limit=MAX_BOARD_ITEMS,
        )
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in raw_items:
        item = _item(value, strict=True)
        assert item is not None
        if item["app_id"] in seen:
            raise _invalid(f"duplicate app in board: {item['app_id']}")
        seen.add(item["app_id"])
        items.append(item)
    return {"version": LAYOUT_VERSION, "items": _sorted(items)}


def dump_layout(layout: dict[str, Any]) -> str:
    return json.dumps(
        layout,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(frozen=True)
class BoardView:
    board_id: str
    name: str
    description: str
    layout: dict[str, Any]
    version: int
    created_at: int
    updated_at: int

    @property
    def item_count(self) -> int:
        return len(self.layout["items"])


class BoardService:
    def __init__(
        self,
        *,
        board_repo: LightAppBoardRepo,
        app_repo: LightAppRepo,
    ) -> None:
        self._board_repo = board_repo
        self._app_repo = app_repo

    def _get(self, board_id: str, user_id: int, *, is_admin: bool) -> LightAppBoardRow:
        row = self._board_repo.get(board_id)
        if row is None:
            raise OctopError(ErrorCode.BOARD_NOT_FOUND, "board not found")
        if row.owner_user_id != user_id and not is_admin:
            raise OctopError(ErrorCode.BOARD_FORBIDDEN, "board not owned by user")
        return row

    def view(self, row: LightAppBoardRow) -> BoardView:
        return BoardView(
            board_id=row.board_id,
            name=row.name,
            description=row.description,
            layout=parse_layout(row.layout_json),
            version=row.version,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def get(self, board_id: str, *, user_id: int, is_admin: bool) -> BoardView:
        return self.view(self._get(board_id, user_id, is_admin=is_admin))

    def list_boards(self, user_id: int) -> list[BoardView]:
        return [self.view(row) for row in self._board_repo.list_for_owner(user_id)]

    def create(self, *, user_id: int, name: str, description: str = "") -> BoardView:
        row = self._board_repo.create(
            owner_user_id=user_id,
            name=_clean_name(name),
            description=_clean_description(description),
        )
        return self.view(row)

    def update(
        self,
        board_id: str,
        *,
        user_id: int,
        is_admin: bool,
        name: str | None = None,
        description: str | None = None,
    ) -> BoardView:
        row = self._get(board_id, user_id, is_admin=is_admin)
        self._board_repo.update_metadata(
            row.board_id,
            name=_clean_name(name) if name is not None else None,
            description=_clean_description(description) if description is not None else None,
        )
        return self.view(self._board_repo.get(row.board_id) or row)

    def delete(self, board_id: str, *, user_id: int, is_admin: bool) -> None:
        row = self._get(board_id, user_id, is_admin=is_admin)
        self._board_repo.delete(row.board_id)

    def set_layout(
        self,
        board_id: str,
        *,
        user_id: int,
        is_admin: bool,
        items: object,
        expected_version: int,
    ) -> BoardView:
        row = self._get(board_id, user_id, is_admin=is_admin)
        layout = validate_layout(items)
        for item in layout["items"]:
            app = self._app_repo.get(item["app_id"])
            accessible = app is not None and (
                app.owner_user_id == row.owner_user_id
                or self._app_repo.permission_for(app.app_id, row.owner_user_id) is not None
            )
            if not accessible:
                raise _invalid(
                    f"MiniApp is not accessible: {item['app_id']}",
                    app_id=item["app_id"],
                )
        if not self._board_repo.set_layout(
            row.board_id,
            dump_layout(layout),
            expected_version,
        ):
            raise OctopError(
                ErrorCode.BOARD_CONFLICT,
                "board changed elsewhere; reload and retry",
            )
        return self.view(self._board_repo.get(row.board_id) or row)


__all__ = [
    "DEFAULT_ITEM_H",
    "DEFAULT_ITEM_W",
    "GRID_COLS",
    "MAX_BOARD_ITEMS",
    "BoardService",
    "BoardView",
    "dump_layout",
    "parse_layout",
    "validate_layout",
]
