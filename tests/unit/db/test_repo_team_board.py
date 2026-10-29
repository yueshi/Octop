"""Unit tests for TeamGoalRepo / TeamTaskRepo and the 022 board tables."""

from __future__ import annotations

from pathlib import Path

import pytest

from octop.infra.agents.teams.task_state import is_valid_task_transition
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.team_goals import TeamGoalRepo
from octop.infra.db.repos.team_tasks import TeamTaskRepo
from octop.infra.db.repos.users import UserRepo


@pytest.fixture
def db(tmp_path: Path) -> SqlitePool:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return pool


@pytest.fixture
def env(db: SqlitePool) -> dict[str, object]:
    user = UserRepo(db).create(username="alice", password_hash="h", role="user")
    agent_repo = AgentRepo(db)
    team_id = "team-01"
    agent_repo.create(agent_id=team_id, user_id=user, name="crew", kind="team")
    member_id = "member-01"
    agent_repo.create(agent_id=member_id, user_id=user, name="worker")
    return {"user": user, "team_id": team_id, "member_id": member_id}


def test_board_tables_exist(db: SqlitePool) -> None:
    with db.connect() as conn:
        names = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        columns = {
            table: {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for table in ("team_goals", "team_tasks", "hitl_pending")
        }
    assert {"team_goals", "team_tasks", "hitl_pending"} <= names
    assert {"goal_id", "team_id", "status", "created_by"} <= columns["team_goals"]
    assert {
        "task_id",
        "team_id",
        "goal_id",
        "assignee",
        "version",
        "due_at",
    } <= columns["team_tasks"]
    assert {"session_key", "kind", "task_id", "status", "expires_at"} <= columns["hitl_pending"]


def test_goal_archive_replaces_active(db: SqlitePool, env: dict[str, object]) -> None:
    repo = TeamGoalRepo(db)
    team_id = str(env["team_id"])
    user = int(env["user"])  # type: ignore[arg-type]
    first = repo.create(team_id=team_id, title="Launch", created_by=user)
    assert first.status == "active"
    assert repo.active(team_id) is not None

    repo.archive_active(team_id)
    second = repo.create(team_id=team_id, title="Grow", created_by=user)
    assert repo.active(team_id) is not None
    assert repo.active(team_id) is not None and repo.active(team_id).goal_id == second.goal_id  # type: ignore[union-attr]
    archived = repo.get(first.goal_id)
    assert archived is not None and archived.status == "archived"


def test_goal_list_by_team_filters_status(db: SqlitePool, env: dict[str, object]) -> None:
    repo = TeamGoalRepo(db)
    team_id = str(env["team_id"])
    user = int(env["user"])  # type: ignore[arg-type]
    repo.create(team_id=team_id, title="a", created_by=user)
    repo.archive_active(team_id)
    repo.create(team_id=team_id, title="b", created_by=user)
    assert len(repo.list_by_team(team_id)) == 2
    assert [g.title for g in repo.list_by_team(team_id, status="active")] == ["b"]


def test_task_create_and_cas_update(db: SqlitePool, env: dict[str, object]) -> None:
    goal_repo = TeamGoalRepo(db)
    task_repo = TeamTaskRepo(db)
    team_id = str(env["team_id"])
    user = int(env["user"])  # type: ignore[arg-type]
    goal = goal_repo.create(team_id=team_id, title="Launch", created_by=user)
    task = task_repo.create(
        team_id=team_id,
        title="Draft brief",
        goal_id=goal.goal_id,
        assignee=str(env["member_id"]),
        priority=2,
        due_at=1234,
    )
    assert task.status == "proposed"
    assert task.version == 1

    updated = task_repo.update(
        task.task_id,
        expected_version=task.version,
        fields={"status": "running", "result_summary": "on track"},
    )
    assert updated is not None
    assert updated.status == "running"
    assert updated.version == 2

    # Stale version → CAS miss, row untouched.
    assert (
        task_repo.update(task.task_id, expected_version=task.version, fields={"status": "done"})
        is None
    )
    fresh = task_repo.get(task.task_id)
    assert fresh is not None and fresh.status == "running" and fresh.version == 2


def test_task_update_missing_fields_is_noop(db: SqlitePool, env: dict[str, object]) -> None:
    task_repo = TeamTaskRepo(db)
    team_id = str(env["team_id"])
    task = task_repo.create(team_id=team_id, title="t")
    same = task_repo.update(task.task_id, expected_version=task.version, fields={})
    assert same is not None and same.version == task.version


def test_task_list_and_count_open(db: SqlitePool, env: dict[str, object]) -> None:
    task_repo = TeamTaskRepo(db)
    team_id = str(env["team_id"])
    task_repo.create(team_id=team_id, title="open", priority=1)
    task_repo.create(team_id=team_id, title="done", status="done")
    closed = task_repo.create(team_id=team_id, title="cancelled", status="cancelled")
    assert task_repo.count_open(team_id) == 1
    assert [t.title for t in task_repo.list_by_team(team_id, status="proposed")] == ["open"]
    assert [t.title for t in task_repo.list_by_team(team_id)] == ["open", "done", "cancelled"]
    assert task_repo.get(closed.task_id) is not None


def test_deleting_team_cascades_tasks(db: SqlitePool, env: dict[str, object]) -> None:
    task_repo = TeamTaskRepo(db)
    team_id = str(env["team_id"])
    task = task_repo.create(team_id=team_id, title="t")
    with db.transaction() as conn:
        conn.execute("DELETE FROM agents WHERE agent_id = ?", (team_id,))
    assert task_repo.get(task.task_id) is None


def test_deleting_goal_nulls_task_goal_id(db: SqlitePool, env: dict[str, object]) -> None:
    goal_repo = TeamGoalRepo(db)
    task_repo = TeamTaskRepo(db)
    team_id = str(env["team_id"])
    user = int(env["user"])  # type: ignore[arg-type]
    goal = goal_repo.create(team_id=team_id, title="g", created_by=user)
    task = task_repo.create(team_id=team_id, title="t", goal_id=goal.goal_id)
    with db.transaction() as conn:
        conn.execute("DELETE FROM team_goals WHERE goal_id = ?", (goal.goal_id,))
    fresh = task_repo.get(task.task_id)
    assert fresh is not None and fresh.goal_id is None


def test_task_transition_table() -> None:
    assert is_valid_task_transition("proposed", "assigned")
    assert is_valid_task_transition("running", "done")
    assert is_valid_task_transition("running", "running")
    assert not is_valid_task_transition("done", "running")
    assert not is_valid_task_transition("proposed", "review")
    assert not is_valid_task_transition("cancelled", "running")
    assert not is_valid_task_transition("proposed", "nope")
