"""Unit tests for the shadow dispatch planner (docs/team-drive-v2.md §7.1, phase 2)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from octop.infra.agents.teams.dispatcher import plan_dispatches
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.team_task_deps import TeamTaskDepsRepo
from octop.infra.db.repos.team_tasks import TeamTaskRepo
from octop.infra.db.repos.users import UserRepo

NOW = 1_800_000_000


@pytest.fixture
def db(tmp_path: Path) -> SqlitePool:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return pool


@pytest.fixture
def team_id(db: SqlitePool) -> str:
    UserRepo(db).create(username="alice", password_hash="h", role="user")
    AgentRepo(db).create(agent_id="team-01", user_id=1, name="crew", kind="team")
    return "team-01"


@pytest.fixture
def repos(db: SqlitePool) -> SimpleNamespace:
    """RepoBundle stand-off mirroring the attributes the dispatcher reads."""
    return SimpleNamespace(
        team_task_repo=TeamTaskRepo(db),
        team_task_deps_repo=TeamTaskDepsRepo(db),
    )


@pytest.fixture
def tasks(repos: SimpleNamespace) -> TeamTaskRepo:
    return repos.team_task_repo


@pytest.fixture
def members(db: SqlitePool) -> None:
    # team_tasks.assignee FK → agents(agent_id).
    AgentRepo(db).create(agent_id="member-a", user_id=1, name="a", kind="expert")
    AgentRepo(db).create(agent_id="member-b", user_id=1, name="b", kind="expert")


def test_empty_team_has_no_steps(repos: SimpleNamespace, team_id: str) -> None:
    assert plan_dispatches(repos, team_id, now=NOW) == []


def test_dispatch_order_priority_then_deadline(
    repos: SimpleNamespace, team_id: str, tasks: TeamTaskRepo
) -> None:
    low = tasks.create(team_id=team_id, title="low", priority=0)
    high = tasks.create(team_id=team_id, title="high", priority=5)
    steps = plan_dispatches(repos, team_id, now=NOW)
    assert [s.task_id for s in steps] == [high.task_id, low.task_id]
    assert all(s.action == "dispatch" for s in steps)


def test_open_dependency_is_skipped_but_ready_task_dispatches(
    repos: SimpleNamespace, team_id: str, tasks: TeamTaskRepo
) -> None:
    first = tasks.create(team_id=team_id, title="first", priority=5)
    second = tasks.create(team_id=team_id, title="second")
    assert repos.team_task_deps_repo.add(second.task_id, first.task_id)
    steps = plan_dispatches(repos, team_id, now=NOW)
    assert [(s.task_id, s.action) for s in steps] == [
        (first.task_id, "dispatch"),
        (second.task_id, "skip_deps"),
    ]


def test_broken_dependency_escalates(
    repos: SimpleNamespace, team_id: str, tasks: TeamTaskRepo
) -> None:
    failed = tasks.create(team_id=team_id, title="failed", priority=5, status="failed")
    dependent = tasks.create(team_id=team_id, title="dependent")
    assert repos.team_task_deps_repo.add(dependent.task_id, failed.task_id)
    steps = plan_dispatches(repos, team_id, now=NOW)
    # The failed task itself is terminal, so only its dependent is projected.
    assert [(s.task_id, s.action) for s in steps] == [(dependent.task_id, "escalate_deps")]
    assert "failed" in steps[0].reason


def test_same_assignee_is_busy_serialized(
    repos: SimpleNamespace, team_id: str, tasks: TeamTaskRepo, members: None
) -> None:
    tasks.create(
        team_id=team_id, title="running", priority=5, assignee="member-a", status="running"
    )
    next_task = tasks.create(team_id=team_id, title="next", assignee="member-a")
    other = tasks.create(team_id=team_id, title="other", assignee="member-b")
    steps = plan_dispatches(repos, team_id, now=NOW)
    by_task = {s.task_id: s.action for s in steps}
    assert by_task[next_task.task_id] == "skip_busy"
    assert by_task[other.task_id] == "dispatch"


def test_window_before_start_waits(
    repos: SimpleNamespace, team_id: str, tasks: TeamTaskRepo
) -> None:
    windowed = tasks.create(team_id=team_id, title="windowed", priority=5)
    tasks.update(windowed.task_id, expected_version=1, fields={"window_start": NOW + 60})
    steps = plan_dispatches(repos, team_id, now=NOW)
    assert [(s.task_id, s.action) for s in steps] == [(windowed.task_id, "wait_window")]


def test_past_deadline_escalates(repos: SimpleNamespace, team_id: str, tasks: TeamTaskRepo) -> None:
    late = tasks.create(team_id=team_id, title="late", priority=5)
    tasks.update(late.task_id, expected_version=1, fields={"due_at": NOW - 60})
    steps = plan_dispatches(repos, team_id, now=NOW)
    assert [(s.task_id, s.action) for s in steps] == [(late.task_id, "escalate_deadline")]


def test_budget_gate_defers_expensive_task(
    repos: SimpleNamespace, team_id: str, tasks: TeamTaskRepo
) -> None:
    cheap = tasks.create(team_id=team_id, title="cheap", priority=5)
    pricey = tasks.create(team_id=team_id, title="pricey")
    tasks.update(pricey.task_id, expected_version=1, fields={"budget_tokens": 1000})
    steps = plan_dispatches(repos, team_id, now=NOW, budget_remaining=500)
    by_task = {s.task_id: s.action for s in steps}
    assert by_task[cheap.task_id] == "dispatch"
    assert by_task[pricey.task_id] == "defer_budget"
    # No budget injected → no gate.
    steps = plan_dispatches(repos, team_id, now=NOW)
    assert {s.task_id: s.action for s in steps}[pricey.task_id] == "dispatch"


def test_max_dispatches_limits_and_defers_rest(
    repos: SimpleNamespace, team_id: str, tasks: TeamTaskRepo
) -> None:
    for i in range(3):
        tasks.create(team_id=team_id, title=f"t{i}", priority=10 - i)
    steps = plan_dispatches(repos, team_id, now=NOW, max_dispatches=2)
    assert [(s.action, s.reason) for s in steps] == [
        ("dispatch", "assignee=auto"),
        ("dispatch", "assignee=auto"),
        ("defer_limit", "max_dispatches=2"),
    ]


def test_concurrency_key_overrides_assignee(
    repos: SimpleNamespace, team_id: str, tasks: TeamTaskRepo, members: None
) -> None:
    busy = tasks.create(
        team_id=team_id, title="busy", priority=5, assignee="member-a", status="assigned"
    )
    tasks.update(busy.task_id, expected_version=1, fields={"concurrency_key": "db-lock"})
    keyed = tasks.create(team_id=team_id, title="keyed", assignee="member-b")
    tasks.update(keyed.task_id, expected_version=1, fields={"concurrency_key": "db-lock"})
    steps = plan_dispatches(repos, team_id, now=NOW)
    by_task = {s.task_id: s.action for s in steps}
    assert by_task[keyed.task_id] == "skip_busy"
