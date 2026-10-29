"""Unit tests for deterministic DecisionPlan application (docs/team-drive-v2.md §6.2)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from octop.config import OctopConfig
from octop.infra.agents.teams.decision import DecisionPlan
from octop.infra.agents.teams.plan_apply import PlanApplyError, apply_decision_plan
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.utils.paths import PathLayout

NOW = 1_800_000_000


class Recorder:
    def __init__(self) -> None:
        self.tasks: list[Any] = []
        self.goals: list[Any] = []

    def record_task_change(self, row: Any, *, actor: str) -> str:
        self.tasks.append((row, actor))
        return "evt"

    def record_goal_change(self, row: Any, *, actor: str) -> str:
        self.goals.append((row, actor))
        return "evt"


@pytest.fixture
def services(tmp_path: Path) -> Any:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    paths = PathLayout(tmp_path)
    paths.ensure_root()
    return build_shared_services(db=pool, paths=paths, config=OctopConfig())


@pytest.fixture
def repos(services: Any) -> Any:
    return services.repos


@pytest.fixture
def team_id(repos: Any) -> str:
    user = repos.user_repo.create(username="alice", password_hash="h", role="user")
    repos.agent_repo.create(agent_id="team-01", user_id=user, name="crew", kind="team")
    repos.agent_repo.create(agent_id="member-1", user_id=user, name="m1", kind="expert")
    return "team-01"


def _plan(**kwargs: Any) -> DecisionPlan:
    kwargs.setdefault("notify", None)
    return DecisionPlan.model_validate(kwargs)


def test_apply_creates_goal_tasks_deps_and_timer(repos: Any, team_id: str) -> None:
    recorder = Recorder()
    plan = _plan(
        goal_ops=[
            {
                "op": "set",
                "title": "Ship",
                "success_criteria": "board live",
                "idempotency_key": "g1",
            }
        ],
        task_ops=[
            {
                "op": "create",
                "task_id": "a",
                "title": "A",
                "assignee": "member-1",
                "priority": 5,
                "idempotency_key": "k1",
            },
            {
                "op": "create",
                "task_id": "b",
                "title": "B",
                "depends_on": ["a"],
                "idempotency_key": "k2",
            },
        ],
        wait_ops=[{"op": "sleep", "task_id": "b", "until": NOW + 60, "idempotency_key": "w1"}],
        notify={"user": True, "text": "Team started"},
    )

    result = apply_decision_plan(repos, team_id, plan, decision_id="d1", recorder=recorder, now=NOW)

    goal = repos.team_goal_repo.active(team_id)
    assert goal is not None and goal.title == "Ship"
    assert result.goal_id == goal.goal_id
    assert set(result.created_tasks) == {"a", "b"}
    a = repos.team_task_repo.get(result.created_tasks["a"])
    b = repos.team_task_repo.get(result.created_tasks["b"])
    assert a is not None and a.priority == 5 and a.goal_id == goal.goal_id
    assert b is not None and b.active_wait_id
    assert repos.team_task_deps_repo.list_for_task(b.task_id) == [a.task_id]
    wait = repos.team_wait_repo.get(b.active_wait_id)
    assert wait is not None and wait.kind == "timer" and wait.due_at == NOW + 60
    assert "Team started" in result.notifications
    assert [actor for _row, actor in recorder.tasks] == ["host:d1", "host:d1"]
    assert [actor for _row, actor in recorder.goals] == ["host:d1"]


def test_apply_task_create_is_idempotent(repos: Any, team_id: str) -> None:
    plan = _plan(task_ops=[{"op": "create", "task_id": "a", "title": "A", "idempotency_key": "k1"}])
    first = apply_decision_plan(repos, team_id, plan, decision_id="d1", now=NOW)
    second = apply_decision_plan(repos, team_id, plan, decision_id="d1", now=NOW)
    assert first.created_tasks == second.created_tasks
    assert len(repos.team_task_repo.list_by_team(team_id)) == 1


def test_apply_update_assign_close_cancel(repos: Any, team_id: str) -> None:
    task = repos.team_task_repo.create(team_id=team_id, title="T")
    other = repos.team_task_repo.create(team_id=team_id, title="U")

    plan = _plan(
        task_ops=[
            {
                "op": "update",
                "task_id": task.task_id,
                "expected_version": task.version,
                "fields": {"status": "running", "priority": 2},
                "idempotency_key": "k1",
            },
            {
                "op": "assign",
                "task_id": other.task_id,
                "assignee": "member-1",
                "concurrency_key": "member-1",
                "idempotency_key": "k2",
            },
        ]
    )
    apply_decision_plan(repos, team_id, plan, decision_id="d1", now=NOW)

    updated = repos.team_task_repo.get(task.task_id)
    assert updated is not None and updated.status == "running" and updated.priority == 2
    assigned = repos.team_task_repo.get(other.task_id)
    assert assigned is not None and assigned.assignee == "member-1"
    assert assigned.concurrency_key == "member-1"

    close = _plan(
        task_ops=[
            {
                "op": "close",
                "task_id": task.task_id,
                "result_summary": "done well",
                "idempotency_key": "k3",
            },
            {
                "op": "cancel",
                "task_id": other.task_id,
                "reason": "superseded",
                "idempotency_key": "k4",
            },
        ]
    )
    apply_decision_plan(repos, team_id, close, decision_id="d2", now=NOW)
    closed = repos.team_task_repo.get(task.task_id)
    assert closed is not None and closed.status == "done"
    assert closed.result_summary == "done well"
    cancelled = repos.team_task_repo.get(other.task_id)
    assert cancelled is not None and cancelled.status == "cancelled"
    assert cancelled.blocked_reason == "superseded"


def test_apply_wait_approval_blocks_task(repos: Any, team_id: str) -> None:
    task = repos.team_task_repo.create(team_id=team_id, title="T")
    plan = _plan(
        wait_ops=[
            {
                "op": "wait_approval",
                "task_id": task.task_id,
                "reason": "needs a decision",
                "idempotency_key": "w1",
            }
        ]
    )
    apply_decision_plan(repos, team_id, plan, decision_id="d1", now=NOW)
    updated = repos.team_task_repo.get(task.task_id)
    assert updated is not None and updated.status == "needs_input"
    assert updated.active_wait_id
    wait = repos.team_wait_repo.get(updated.active_wait_id)
    assert wait is not None and wait.kind == "approval"


def test_apply_rejects_invalid_plan(repos: Any, team_id: str) -> None:
    plan = _plan(
        task_ops=[
            {
                "op": "create",
                "task_id": "a",
                "title": "A",
                "assignee": "ghost",
                "idempotency_key": "k1",
            }
        ]
    )
    with pytest.raises(PlanApplyError):
        apply_decision_plan(repos, team_id, plan, decision_id="d1", now=NOW)
    assert repos.team_task_repo.list_by_team(team_id) == []


def test_apply_noop_does_nothing(repos: Any, team_id: str) -> None:
    result = apply_decision_plan(
        repos, team_id, _plan(noop=True, rationale="quiet"), decision_id="d1", now=NOW
    )
    assert result.applied_ops == []
    assert repos.team_task_repo.list_by_team(team_id) == []
