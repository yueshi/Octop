"""Unit tests for workbench board layout validation and access rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from octop.infra.apps.boards import (
    MAX_BOARD_ITEMS,
    BoardService,
    parse_layout,
    validate_layout,
)
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.apps import LightAppRepo
from octop.infra.db.repos.boards import LightAppBoardRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.utils.ulid import new_ulid


@pytest.fixture
def db(tmp_path: Path) -> SqlitePool:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return pool


@pytest.fixture
def env(db: SqlitePool) -> dict[str, object]:
    users = UserRepo(db)
    alice = users.create(username="alice", password_hash="h", role="user")
    bob = users.create(username="bob", password_hash="h", role="user")
    admin = users.create(username="admin", password_hash="h", role="admin")
    agent_id = new_ulid()
    AgentRepo(db).create(agent_id=agent_id, user_id=alice, name="bot")
    apps = LightAppRepo(db)
    alice_app = apps.create(
        agent_id=agent_id,
        owner_user_id=alice,
        name="Alice app",
    )
    bob_app = apps.create(
        agent_id=agent_id,
        owner_user_id=bob,
        name="Bob app",
    )
    return {
        "alice": alice,
        "bob": bob,
        "admin": admin,
        "alice_app": alice_app.app_id,
        "bob_app": bob_app.app_id,
    }


@pytest.fixture
def service(db: SqlitePool) -> BoardService:
    return BoardService(board_repo=LightAppBoardRepo(db), app_repo=LightAppRepo(db))


def _item(app_id: str, x: int = 0, y: int = 0, w: int = 4, h: int = 4) -> dict[str, object]:
    return {"app_id": app_id, "x": x, "y": y, "w": w, "h": h}


def _code(exc: pytest.ExceptionInfo[OctopError]) -> ErrorCode:
    return exc.value.code


def test_validate_layout_sorts_by_position(env: dict[str, object]) -> None:
    layout = validate_layout(
        [
            _item(str(env["bob_app"]), x=8, y=0),
            _item(str(env["alice_app"]), x=0, y=2),
        ]
    )
    assert [item["app_id"] for item in layout["items"]] == [env["bob_app"], env["alice_app"]]


def test_validate_layout_rejects_duplicate_apps(env: dict[str, object]) -> None:
    with pytest.raises(OctopError) as exc:
        validate_layout([_item(str(env["alice_app"])), _item(str(env["alice_app"]), x=4)])
    assert _code(exc) is ErrorCode.BOARD_LAYOUT_INVALID


def test_validate_layout_rejects_out_of_grid_rectangle(env: dict[str, object]) -> None:
    with pytest.raises(OctopError) as exc:
        validate_layout([_item(str(env["alice_app"]), x=10, w=4)])
    assert _code(exc) is ErrorCode.BOARD_LAYOUT_INVALID


def test_validate_layout_rejects_too_many_items(env: dict[str, object]) -> None:
    items = [_item(f"app_{index}") for index in range(MAX_BOARD_ITEMS + 1)]
    with pytest.raises(OctopError) as exc:
        validate_layout(items)
    assert _code(exc) is ErrorCode.BOARD_LAYOUT_INVALID


def test_parse_layout_drops_bad_rows_instead_of_failing() -> None:
    layout = parse_layout(
        '{"version":1,"items":[{"app_id":"a","x":0,"y":0,"w":4,"h":4},'
        '{"app_id":"b","x":-1,"y":0,"w":4,"h":4},"junk"]}'
    )
    assert [item["app_id"] for item in layout["items"]] == ["a"]


def test_parse_layout_survives_malformed_json() -> None:
    assert parse_layout("{not json") == {"version": 1, "items": []}


def test_owner_can_create_rename_and_delete(service: BoardService, env: dict[str, object]) -> None:
    view = service.create(user_id=env["alice"], name=" Home ", description=" desk ")  # type: ignore[arg-type]
    assert view.name == "Home"
    assert view.description == "desk"
    assert view.version == 1
    assert view.item_count == 0

    renamed = service.update(
        view.board_id,
        user_id=env["alice"],  # type: ignore[arg-type]
        is_admin=False,
        name="Renamed",
    )
    assert renamed.name == "Renamed"

    service.delete(view.board_id, user_id=env["alice"], is_admin=False)  # type: ignore[arg-type]
    with pytest.raises(OctopError) as exc:
        service.get(view.board_id, user_id=env["alice"], is_admin=False)  # type: ignore[arg-type]
    assert _code(exc) is ErrorCode.BOARD_NOT_FOUND


def test_other_user_cannot_read_or_change_board(
    service: BoardService, env: dict[str, object]
) -> None:
    view = service.create(user_id=env["alice"], name="Home")  # type: ignore[arg-type]

    with pytest.raises(OctopError) as exc:
        service.get(view.board_id, user_id=env["bob"], is_admin=False)  # type: ignore[arg-type]
    assert _code(exc) is ErrorCode.BOARD_FORBIDDEN

    with pytest.raises(OctopError) as exc:
        service.set_layout(
            view.board_id,
            user_id=env["bob"],  # type: ignore[arg-type]
            is_admin=False,
            items=[],
            expected_version=1,
        )
    assert _code(exc) is ErrorCode.BOARD_FORBIDDEN


def test_admin_can_read_other_users_board(service: BoardService, env: dict[str, object]) -> None:
    view = service.create(user_id=env["alice"], name="Home")  # type: ignore[arg-type]
    assert service.get(view.board_id, user_id=env["admin"], is_admin=True).name == "Home"  # type: ignore[arg-type]


def test_set_layout_saves_and_bumps_version(service: BoardService, env: dict[str, object]) -> None:
    view = service.create(user_id=env["alice"], name="Home")  # type: ignore[arg-type]

    saved = service.set_layout(
        view.board_id,
        user_id=env["alice"],  # type: ignore[arg-type]
        is_admin=False,
        items=[_item(str(env["alice_app"]))],
        expected_version=1,
    )

    assert saved.version == 2
    assert saved.layout["items"][0]["app_id"] == env["alice_app"]


def test_set_layout_rejects_app_the_owner_cannot_access(
    service: BoardService, env: dict[str, object]
) -> None:
    view = service.create(user_id=env["alice"], name="Home")  # type: ignore[arg-type]

    with pytest.raises(OctopError) as exc:
        service.set_layout(
            view.board_id,
            user_id=env["alice"],  # type: ignore[arg-type]
            is_admin=False,
            items=[_item(str(env["bob_app"]))],
            expected_version=1,
        )
    assert _code(exc) is ErrorCode.BOARD_LAYOUT_INVALID
    assert service.get(view.board_id, user_id=env["alice"], is_admin=False).version == 1  # type: ignore[arg-type]


def test_set_layout_conflict_when_version_is_stale(
    service: BoardService, env: dict[str, object]
) -> None:
    view = service.create(user_id=env["alice"], name="Home")  # type: ignore[arg-type]
    service.set_layout(
        view.board_id,
        user_id=env["alice"],  # type: ignore[arg-type]
        is_admin=False,
        items=[],
        expected_version=1,
    )

    with pytest.raises(OctopError) as exc:
        service.set_layout(
            view.board_id,
            user_id=env["alice"],  # type: ignore[arg-type]
            is_admin=False,
            items=[],
            expected_version=1,
        )
    assert _code(exc) is ErrorCode.BOARD_CONFLICT
