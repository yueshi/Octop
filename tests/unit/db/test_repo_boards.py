"""Unit tests for LightAppBoardRepo and the miniapp_boards migration."""

from __future__ import annotations

from pathlib import Path

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.boards import EMPTY_BOARD_LAYOUT, LightAppBoardRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.utils.ulid import new_ulid


@pytest.fixture
def db(tmp_path: Path) -> SqlitePool:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return pool


@pytest.fixture
def ids(db: SqlitePool) -> dict[str, object]:
    users = UserRepo(db)
    alice = users.create(username="alice", password_hash="h", role="user")
    bob = users.create(username="bob", password_hash="h", role="user")
    agent_id = new_ulid()
    AgentRepo(db).create(agent_id=agent_id, user_id=alice, name="bot")
    return {"alice": alice, "bob": bob, "agent_id": agent_id}


def test_miniapp_boards_table_exists(db: SqlitePool) -> None:
    with db.connect() as conn:
        names = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(miniapp_boards)")}
    assert "miniapp_boards" in names
    assert {
        "board_id",
        "owner_user_id",
        "name",
        "description",
        "layout_json",
        "version",
        "created_at",
        "updated_at",
    } <= columns


def test_migration_repairs_missing_board_table(db: SqlitePool) -> None:
    with db.transaction() as conn:
        conn.execute("DROP TABLE miniapp_boards")
    run_migrations(db)
    with db.connect() as conn:
        names = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "miniapp_boards" in names


def test_create_get_and_default_layout(db: SqlitePool, ids: dict[str, object]) -> None:
    repo = LightAppBoardRepo(db)
    row = repo.create(owner_user_id=ids["alice"], name="Home")  # type: ignore[arg-type]

    assert row.board_id.startswith("board_")
    assert row.version == 1
    assert row.layout_json == EMPTY_BOARD_LAYOUT

    loaded = repo.get(row.board_id)
    assert loaded is not None
    assert loaded.name == "Home"
    assert loaded.owner_user_id == ids["alice"]


def test_list_is_scoped_to_owner(db: SqlitePool, ids: dict[str, object]) -> None:
    repo = LightAppBoardRepo(db)
    repo.create(owner_user_id=ids["alice"], name="A")  # type: ignore[arg-type]
    repo.create(owner_user_id=ids["alice"], name="B")  # type: ignore[arg-type]
    repo.create(owner_user_id=ids["bob"], name="C")  # type: ignore[arg-type]

    names = {row.name for row in repo.list_for_owner(ids["alice"])}  # type: ignore[arg-type]
    assert names == {"A", "B"}
    assert [row.name for row in repo.list_for_owner(ids["bob"])] == ["C"]  # type: ignore[arg-type]


def test_update_metadata(db: SqlitePool, ids: dict[str, object]) -> None:
    repo = LightAppBoardRepo(db)
    row = repo.create(owner_user_id=ids["alice"], name="Old")  # type: ignore[arg-type]

    repo.update_metadata(row.board_id, name="New", description="desc")

    loaded = repo.get(row.board_id)
    assert loaded is not None
    assert (loaded.name, loaded.description) == ("New", "desc")


def test_set_layout_is_compare_and_swap(db: SqlitePool, ids: dict[str, object]) -> None:
    repo = LightAppBoardRepo(db)
    row = repo.create(owner_user_id=ids["alice"], name="Home")  # type: ignore[arg-type]
    layout = '{"version":1,"items":[]}'

    assert repo.set_layout(row.board_id, layout, expected_version=1) is True
    assert repo.get(row.board_id).version == 2  # type: ignore[union-attr]
    # A stale tab still holding version 1 loses the race.
    assert repo.set_layout(row.board_id, layout, expected_version=1) is False
    assert repo.get(row.board_id).version == 2  # type: ignore[union-attr]


def test_delete_removes_board(db: SqlitePool, ids: dict[str, object]) -> None:
    repo = LightAppBoardRepo(db)
    row = repo.create(owner_user_id=ids["alice"], name="Home")  # type: ignore[arg-type]
    repo.delete(row.board_id)
    assert repo.get(row.board_id) is None


def test_deleting_user_cascades_boards(db: SqlitePool, ids: dict[str, object]) -> None:
    repo = LightAppBoardRepo(db)
    row = repo.create(owner_user_id=ids["bob"], name="Temp")  # type: ignore[arg-type]
    with db.transaction() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (ids["bob"],))
    assert repo.get(row.board_id) is None
