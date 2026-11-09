"""Unit tests for LightAppRepo and migration 022."""

from __future__ import annotations

from pathlib import Path

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.apps import LightAppRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.utils.ulid import new_ulid


@pytest.fixture
def db(tmp_path: Path) -> SqlitePool:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return pool


@pytest.fixture
def ids(db: SqlitePool) -> dict[str, object]:
    user_repo = UserRepo(db)
    alice = user_repo.create(username="alice", password_hash="h", role="user")
    bob = user_repo.create(username="bob", password_hash="h", role="user")
    agent_id = new_ulid()
    AgentRepo(db).create(agent_id=agent_id, user_id=alice, name="bot")
    return {"alice": alice, "bob": bob, "agent_id": agent_id}


def test_light_app_tables_exist(db: SqlitePool) -> None:
    with db.connect() as conn:
        names = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        version = conn.execute("SELECT version FROM _schema_version").fetchone()[0]
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(light_apps)")}
    assert {"light_apps", "light_app_collaborators"} <= names
    assert version == 30
    assert {"app_id", "agent_id", "owner_user_id", "row_count"} <= columns


def test_run_migrations_repairs_missing_light_app_tables(db: SqlitePool) -> None:
    with db.transaction() as conn:
        conn.execute("DROP TABLE light_app_collaborators")
        conn.execute("DROP TABLE light_apps")
    run_migrations(db)
    with db.connect() as conn:
        names = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"light_apps", "light_app_collaborators"} <= names


def test_run_migrations_adds_file_size_columns_to_v25_databases(db: SqlitePool) -> None:
    """A database that already recorded v25 predates the file-size columns."""
    with db.transaction() as conn:
        conn.execute("ALTER TABLE light_apps DROP COLUMN html_bytes")
        conn.execute("ALTER TABLE light_apps DROP COLUMN data_bytes")
    run_migrations(db)
    with db.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(light_apps)")}
    assert {"html_bytes", "data_bytes"} <= columns


def test_create_get_and_lists(db: SqlitePool, ids: dict[str, object]) -> None:
    repo = LightAppRepo(db)
    alice = int(ids["alice"])
    agent_id = str(ids["agent_id"])
    app_id = repo.allocate_id()
    assert app_id.startswith("app_")
    assert repo.get(app_id) is None

    row = repo.create(
        app_id=app_id,
        agent_id=agent_id,
        owner_user_id=alice,
        name="Tracker",
        description="things",
    )
    assert row.pk >= 1
    assert row.row_count == 0
    assert repo.get(app_id) == row
    assert repo.list_owned(alice) == [row]
    assert repo.list_all() == [row]
    assert repo.list_for_agent(agent_id, alice) == [row]
    assert repo.list_for_agent(agent_id, alice + 1) == []
    assert repo.count_for_owner(alice) == 1


def test_metadata_updates_and_delete(db: SqlitePool, ids: dict[str, object]) -> None:
    repo = LightAppRepo(db)
    row = repo.create(
        agent_id=str(ids["agent_id"]),
        owner_user_id=int(ids["alice"]),
        name="Tracker",
    )
    repo.update_metadata(row.app_id, name="Renamed", description="")
    updated = repo.get(row.app_id)
    assert updated is not None
    assert updated.name == "Renamed"
    assert updated.description == ""

    repo.set_row_count(row.app_id, 7)
    assert repo.get(row.app_id).row_count == 7

    with db.transaction() as conn:
        conn.execute("UPDATE light_apps SET updated_at = 1 WHERE app_id = ?", (row.app_id,))
    repo.set_row_count(row.app_id, 9, touch=False)
    cached = repo.get(row.app_id)
    assert cached is not None
    assert cached.row_count == 9
    assert cached.updated_at == 1

    repo.touch(row.app_id)
    assert repo.get(row.app_id).updated_at >= row.updated_at

    repo.delete(row.app_id)
    assert repo.get(row.app_id) is None


def test_collaborators_replace_and_delete_cascade(db: SqlitePool, ids: dict[str, object]) -> None:
    repo = LightAppRepo(db)
    alice = int(ids["alice"])
    bob = int(ids["bob"])
    row = repo.create(
        agent_id=str(ids["agent_id"]),
        owner_user_id=alice,
        name="Tracker",
    )

    repo.set_collaborators(row.app_id, [(bob, "view")])
    assert repo.permission_for(row.app_id, bob) == "view"
    assert repo.permission_for(row.app_id, alice) is None
    assert [(c.user_id, c.permission) for c in repo.list_collaborators(row.app_id)] == [
        (bob, "view")
    ]
    assert repo.list_shared(bob) == [row]
    assert repo.list_shared(alice) == []

    carol = UserRepo(db).create(username="carol", password_hash="h", role="user")
    repo.set_collaborators(row.app_id, [(carol, "edit_data")])
    assert repo.permission_for(row.app_id, bob) is None
    assert repo.permission_for(row.app_id, carol) == "edit_data"

    repo.delete(row.app_id)
    with db.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM light_app_collaborators WHERE app_id = ?",
            (row.app_id,),
        ).fetchone()[0]
    assert count == 0


def test_source_crud_and_run_state(db: SqlitePool, ids: dict[str, object]) -> None:
    repo = LightAppRepo(db)
    app = repo.create(
        agent_id=str(ids["agent_id"]),
        owner_user_id=int(ids["alice"]),
        name="Tracker",
    )

    inserted = repo.insert_source(
        source_id="src_1",
        app_id=app.app_id,
        table_key="main",
        kind="workspace_file",
        mode="append",
        schedule_seconds=60,
        params_json='{"path": "inbound/imports/a.csv"}',
        mapping_json='{"name": "title"}',
        created_by=int(ids["alice"]),
    )
    assert inserted.source_id == "src_1"
    assert inserted.last_run_at is None and inserted.last_status is None
    assert repo.get_source(app.app_id, "src_1") == inserted
    assert repo.list_sources(app.app_id) == [inserted]

    # A table holds at most one binding at the DB level too.
    from sqlite3 import IntegrityError

    with pytest.raises(IntegrityError):
        repo.insert_source(
            source_id="src_2",
            app_id=app.app_id,
            table_key="main",
            kind="workspace_file",
            mode="append",
            schedule_seconds=0,
            params_json="{}",
            mapping_json="{}",
            created_by=int(ids["alice"]),
        )

    repo.set_source_run_state(
        "src_1", status="ok", run_at=1000, row_count=7, next_run_at=1060, fail_count=0
    )
    refreshed = repo.get_source(app.app_id, "src_1")
    assert refreshed is not None
    assert refreshed.last_status == "ok"
    assert refreshed.last_run_at == 1000
    assert refreshed.last_row_count == 7
    assert refreshed.next_run_at == 1060
    assert refreshed.fail_count == 0

    repo.set_source_run_state(
        "src_1", status="error", run_at=2000, error="boom", next_run_at=2060, fail_count=1
    )
    refreshed = repo.get_source(app.app_id, "src_1")
    assert refreshed is not None
    assert refreshed.last_status == "error" and refreshed.last_error == "boom"
    assert refreshed.fail_count == 1

    # Due when next_run_at passes; the claim pushes it forward atomically.
    assert repo.list_due_sources(now=2059) == []
    assert repo.claim_source("src_1", now=2060, schedule_seconds=60) is True
    assert repo.claim_source("src_1", now=2060, schedule_seconds=60) is False
    assert repo.list_due_sources(now=2119) == []
    assert [item.source_id for item in repo.list_due_sources(now=2120)] == ["src_1"]

    # Update resets run state.
    updated = repo.update_source(
        app.app_id,
        "src_1",
        table_key="main",
        kind="workspace_file",
        mode="replace",
        schedule_seconds=0,
        params_json='{"path": "inbound/imports/b.csv"}',
        mapping_json="{}",
    )
    assert updated.mode == "replace"
    assert updated.last_status is None and updated.last_run_at is None
    assert updated.next_run_at is None and updated.fail_count == 0
    assert repo.list_due_sources(now=10**9) == []

    assert repo.delete_source(app.app_id, "src_1") is True
    assert repo.delete_source(app.app_id, "src_1") is False
    assert repo.list_sources(app.app_id) == []


def test_source_delete_cascades_with_app(db: SqlitePool, ids: dict[str, object]) -> None:
    repo = LightAppRepo(db)
    app = repo.create(
        agent_id=str(ids["agent_id"]),
        owner_user_id=int(ids["alice"]),
        name="Tracker",
    )
    repo.insert_source(
        source_id="src_1",
        app_id=app.app_id,
        table_key="main",
        kind="external",
        mode="append",
        schedule_seconds=0,
        params_json='{"instance_id": "i1", "tool": "search"}',
        mapping_json="{}",
        created_by=int(ids["alice"]),
    )
    repo.delete(app.app_id)
    assert repo.list_sources(app.app_id) == []
