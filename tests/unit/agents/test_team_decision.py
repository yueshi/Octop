"""Unit tests for the shadow decision unit (docs/team-drive-v2.md §6, phase 2)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from octop.infra.agents.teams import decision as decision_mod
from octop.infra.agents.teams.decision import (
    DecisionPlan,
    DecisionRunner,
    build_decision_snapshot,
    is_judgment_event,
    parse_decision_plan,
    validate_decision_plan,
)
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.team_decisions import TeamDecisionRepo
from octop.infra.db.repos.team_events import TeamEventRepo
from octop.infra.db.repos.team_goals import TeamGoalRepo
from octop.infra.db.repos.team_runtime import TeamRuntimeRepo
from octop.infra.db.repos.team_task_deps import TeamTaskDepsRepo
from octop.infra.db.repos.team_tasks import TeamTaskRepo
from octop.infra.db.repos.team_waits import TeamWaitRepo
from octop.infra.db.repos.usage import UsageRepo
from octop.infra.db.repos.users import UserRepo


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


class Repos:
    """Namespace mirroring the RepoBundle attributes the runner touches."""

    def __init__(self, db: SqlitePool) -> None:
        self.team_runtime_repo = TeamRuntimeRepo(db)
        self.team_decision_repo = TeamDecisionRepo(db)
        self.team_task_repo = TeamTaskRepo(db)
        self.team_goal_repo = TeamGoalRepo(db)
        self.team_event_repo = TeamEventRepo(db)
        self.team_task_deps_repo = TeamTaskDepsRepo(db)
        self.team_wait_repo = TeamWaitRepo(db)
        self.agent_repo = AgentRepo(db)
        self.usage_repo = UsageRepo(db)


@pytest.fixture
def repos(db: SqlitePool) -> Repos:
    return Repos(db)


# -- judgment rules ------------------------------------------------------------


def test_is_judgment_event_rules() -> None:
    assert is_judgment_event("goal.changed", None)
    assert is_judgment_event("external.event", {"summary": "订单 #123 已创建"})
    assert is_judgment_event("member.replied", {"status": "failed"})
    assert not is_judgment_event("member.replied", {"status": "done"})
    assert is_judgment_event("approval.resolved", {"status": "rejected"})
    assert not is_judgment_event("approval.resolved", {"status": "approved", "task_id": "t1"})
    # Team-level approval: nothing to resume, so the plan needs a decision.
    assert is_judgment_event("approval.resolved", {"status": "approved"})
    assert not is_judgment_event("task.changed", {"status": "running"})


# -- plan parsing --------------------------------------------------------------


def test_parse_decision_plan_accepts_plain_fenced_and_embedded() -> None:
    plan_json = json.dumps({"noop": True, "rationale": "nothing to do"})
    for text in (
        plan_json,
        f"```json\n{plan_json}\n```",
        f"Here is my plan:\n{plan_json}\nThanks!",
    ):
        parsed = parse_decision_plan(text)
        assert parsed.noop is True
        assert parsed.rationale == "nothing to do"


def test_parse_decision_plan_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_decision_plan("no json here at all")


def test_decision_plan_requires_idempotency_keys() -> None:
    with pytest.raises(ValueError):
        DecisionPlan.model_validate({"task_ops": [{"op": "create", "task_id": "t1"}]})


# -- snapshot ------------------------------------------------------------------


def test_snapshot_orders_filters_and_caps(db: SqlitePool, team_id: str, repos: Repos) -> None:
    goal = repos.team_goal_repo.create(
        team_id=team_id, title="G", created_by=1, description="d", success_criteria="sc"
    )
    tasks = repos.team_task_repo
    for i in range(30):
        tasks.create(team_id=team_id, title=f"t{i}", goal_id=goal.goal_id, priority=34 - i)
    done = tasks.create(team_id=team_id, title="done", status="done")
    # 30 fillers + this one = 31 open; lowest priority gets cut by the cap.
    extra = tasks.create(team_id=team_id, title="cut", priority=-1)
    long_spec = tasks.create(team_id=team_id, title="long", spec="x" * 900, priority=100)
    repos.team_event_repo.append(
        team_id,
        type="member.replied",
        dedupe_key="inbox:1",
        actor="member:m",
        payload={"status": "failed", "excerpt": "boom " * 100},
    )
    repos.team_event_repo.append(
        team_id,
        type="task.changed",
        dedupe_key="task:1:v1",
        actor="host",
        payload={"status": "running"},
    )

    snap = build_decision_snapshot(repos, team_id)  # type: ignore[arg-type]

    assert snap["goal"]["goal_id"] == goal.goal_id
    assert len(snap["tasks"]) == 30
    priorities = [t["priority"] for t in snap["tasks"]]
    assert priorities == sorted(priorities, reverse=True)
    assert all(t["task_id"] not in (done.task_id, extra.task_id) for t in snap["tasks"])
    long_entry = next(t for t in snap["tasks"] if t["task_id"] == long_spec.task_id)
    assert len(long_entry["spec_excerpt"]) == 500
    # Judgment-only events, chronological, excerpt bounded.
    assert len(snap["events"]) == 1
    assert snap["events"][0]["type"] == "member.replied"
    assert len(snap["events"][0]["excerpt"]) == 200
    assert snap["metrics"]["open_count"] == 32
    assert snap["metrics"]["done_today"] == 1
    assert snap["policy"] == {"max_new_tasks": 3, "max_dispatches": 5}


def test_snapshot_without_runtime_or_goal(db: SqlitePool, team_id: str, repos: Repos) -> None:
    snap = build_decision_snapshot(repos, team_id)  # type: ignore[arg-type]
    assert snap["runtime"]["status"] == "idle"
    assert snap["goal"] is None
    assert snap["tasks"] == []


def test_snapshot_surfaces_external_event_summary(
    db: SqlitePool, team_id: str, repos: Repos
) -> None:
    repos.team_event_repo.append(
        team_id,
        type="external.event",
        dedupe_key="external:src-1:evt-1",
        actor="external:src-1",
        payload={"summary": "订单 #123 已创建，金额 ¥1,280"},
    )
    snap = build_decision_snapshot(repos, team_id)  # type: ignore[arg-type]
    assert [event["type"] for event in snap["events"]] == ["external.event"]
    assert snap["events"][0]["excerpt"] == "订单 #123 已创建，金额 ¥1,280"


# -- plan validation -----------------------------------------------------------


def _member(db: SqlitePool) -> None:
    AgentRepo(db).create(agent_id="member-1", user_id=1, name="m1", kind="expert")


def _plan(**kwargs: Any) -> DecisionPlan:
    kwargs.setdefault("noop", False)
    return DecisionPlan.model_validate(kwargs)


def test_validate_accepts_create_with_deps_and_assignee(
    db: SqlitePool, team_id: str, repos: Repos
) -> None:
    _member(db)
    plan = _plan(
        task_ops=[
            {
                "op": "create",
                "task_id": "local-a",
                "title": "A",
                "assignee": "member-1",
                "idempotency_key": "k1",
            },
            {
                "op": "create",
                "task_id": "local-b",
                "title": "B",
                "depends_on": ["local-a"],
                "idempotency_key": "k2",
            },
        ]
    )
    assert validate_decision_plan(plan, repos, team_id) == []  # type: ignore[arg-type]


def test_validate_rejects_duplicates_unknowns_and_cycles(
    db: SqlitePool, team_id: str, repos: Repos
) -> None:
    plan = _plan(
        task_ops=[
            {"op": "create", "task_id": "a", "title": "A", "idempotency_key": "k1"},
            {"op": "create", "task_id": "a", "title": "A2", "idempotency_key": "k2"},
            {
                "op": "create",
                "task_id": "b",
                "title": "B",
                "assignee": "ghost",
                "depends_on": ["missing"],
                "idempotency_key": "k3",
            },
            {"op": "create", "task_id": "c", "idempotency_key": "k4"},
        ]
    )
    violations = " ".join(validate_decision_plan(plan, repos, team_id))  # type: ignore[arg-type]
    assert "duplicate local key" in violations
    assert "unknown assignee" in violations
    assert "unknown depends_on" in violations
    assert "title required" in violations


def test_validate_detects_create_dependency_cycles(
    db: SqlitePool, team_id: str, repos: Repos
) -> None:
    plan = _plan(
        task_ops=[
            {
                "op": "create",
                "task_id": "a",
                "title": "A",
                "depends_on": ["b"],
                "idempotency_key": "k1",
            },
            {
                "op": "create",
                "task_id": "b",
                "title": "B",
                "depends_on": ["a"],
                "idempotency_key": "k2",
            },
        ]
    )
    violations = validate_decision_plan(plan, repos, team_id)  # type: ignore[arg-type]
    assert any("cycle" in v for v in violations)


def test_validate_update_rules(db: SqlitePool, team_id: str, repos: Repos) -> None:
    task = repos.team_task_repo.create(team_id=team_id, title="T", status="proposed")
    good = _plan(
        task_ops=[
            {
                "op": "update",
                "task_id": task.task_id,
                "expected_version": task.version,
                "fields": {"status": "assigned"},
                "idempotency_key": "k1",
            }
        ]
    )
    assert validate_decision_plan(good, repos, team_id) == []  # type: ignore[arg-type]

    no_version = _plan(
        task_ops=[{"op": "update", "task_id": task.task_id, "fields": {}, "idempotency_key": "k1"}]
    )
    assert "expected_version required" in " ".join(
        validate_decision_plan(no_version, repos, team_id)  # type: ignore[arg-type]
    )

    stale = _plan(
        task_ops=[
            {
                "op": "update",
                "task_id": task.task_id,
                "expected_version": task.version + 5,
                "fields": {},
                "idempotency_key": "k1",
            }
        ]
    )
    assert "stale expected_version" in " ".join(
        validate_decision_plan(stale, repos, team_id)  # type: ignore[arg-type]
    )

    bad_field = _plan(
        task_ops=[
            {
                "op": "update",
                "task_id": task.task_id,
                "expected_version": task.version,
                "fields": {"spec": "rewrite"},
                "idempotency_key": "k1",
            }
        ]
    )
    assert "not allowed" in " ".join(
        validate_decision_plan(bad_field, repos, team_id)  # type: ignore[arg-type]
    )

    close_from_proposed = _plan(
        task_ops=[
            {
                "op": "close",
                "task_id": task.task_id,
                "result_summary": "done",
                "idempotency_key": "k1",
            }
        ]
    )
    assert "illegal transition" in " ".join(
        validate_decision_plan(close_from_proposed, repos, team_id)  # type: ignore[arg-type]
    )

    unknown = _plan(task_ops=[{"op": "close", "task_id": "missing", "idempotency_key": "k1"}])
    assert "unknown task" in " ".join(
        validate_decision_plan(unknown, repos, team_id)  # type: ignore[arg-type]
    )


def test_validate_goal_ops(db: SqlitePool, team_id: str, repos: Repos) -> None:
    plan = _plan(goal_ops=[{"op": "update", "goal_id": "missing", "idempotency_key": "k1"}])
    assert "unknown goal" in " ".join(
        validate_decision_plan(plan, repos, team_id)  # type: ignore[arg-type]
    )


# -- runner lifecycle ----------------------------------------------------------


def _runner(repos: Repos, plan_fn: Any, **kwargs: Any) -> DecisionRunner:
    return DecisionRunner(repos=repos, plan_fn=plan_fn, **kwargs)  # type: ignore[arg-type]


def _ok_plan_fn(payload: dict[str, Any], calls: list[str]) -> Any:
    async def plan_fn(prompt: str, team_id: str) -> tuple[str, dict[str, int] | None]:
        calls.append(prompt)
        return (
            json.dumps(payload),
            {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        )

    return plan_fn


async def test_runner_commits_valid_plan(db: SqlitePool, team_id: str, repos: Repos) -> None:
    repos.team_runtime_repo.ensure(team_id)
    calls: list[str] = []
    runner = _runner(repos, _ok_plan_fn({"noop": True, "rationale": "quiet"}, calls))

    decision_id = await runner.run(team_id, trigger_reason="goal.changed")
    assert decision_id is not None
    row = repos.team_decision_repo.get(decision_id)
    assert row is not None
    assert row.status == "committed"
    assert row.cost_tokens == 120
    assert row.snapshot_hash
    assert row.plan is not None
    assert row.plan["plan"]["noop"] is True
    assert "dispatch_projection" in row.plan

    # Usage attributed to the team owner; cooldown stamped.
    runtime = repos.team_runtime_repo.get(team_id)
    assert runtime is not None and runtime.last_decision_at is not None

    # Cooldown: an immediately following run is skipped.
    assert await runner.run(team_id, trigger_reason="goal.changed") is None
    assert len(calls) == 1


async def test_runner_records_usage_row(db: SqlitePool, team_id: str, repos: Repos) -> None:
    repos.team_runtime_repo.ensure(team_id)
    runner = _runner(repos, _ok_plan_fn({"noop": True}, []))
    await runner.run(team_id, trigger_reason="goal.changed")
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT source, team_id, user_id, total_tokens FROM usage_log"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["source"] == "team_decision"
    assert rows[0]["team_id"] == team_id
    assert rows[0]["total_tokens"] == 120


async def test_runner_ignores_non_numeric_usage_details(
    db: SqlitePool, team_id: str, repos: Repos
) -> None:
    """Provider usage metadata may contain nested dicts; they must not crash the runner."""
    repos.team_runtime_repo.ensure(team_id)

    async def plan_fn(prompt: str, team_id: str) -> tuple[str, dict[str, Any] | None]:
        return (
            json.dumps({"noop": True, "rationale": "external event noted"}),
            {
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
                "input_token_details": {"cache_read": 7},
                "output_token_details": {"reasoning": 3},
            },
        )

    runner = _runner(repos, plan_fn)
    decision_id = await runner.run(team_id, trigger_reason="external.event")
    assert decision_id is not None
    row = repos.team_decision_repo.get(decision_id)
    assert row is not None
    assert row.status == "committed"
    assert row.cost_tokens == 120


async def test_runner_no_usage_without_owner(db: SqlitePool, repos: Repos) -> None:
    UserRepo(db).create(username="bob", password_hash="h", role="user")
    AgentRepo(db).create(agent_id="team-x", user_id=None, name="x", kind="team")
    repos.team_runtime_repo.ensure("team-x")
    runner = _runner(repos, _ok_plan_fn({"noop": True}, []))
    await runner.run("team-x", trigger_reason="goal.changed")
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM usage_log").fetchall()
    assert rows == []


async def test_runner_marks_failed_after_two_bad_replies(
    db: SqlitePool, team_id: str, repos: Repos
) -> None:
    repos.team_runtime_repo.ensure(team_id)
    calls: list[str] = []

    async def plan_fn(prompt: str, team_id: str) -> tuple[str, dict[str, int] | None]:
        calls.append(prompt)
        return ("I cannot comply", {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})

    runner = _runner(repos, plan_fn)
    decision_id = await runner.run(team_id, trigger_reason="goal.changed")
    assert decision_id is not None
    row = repos.team_decision_repo.get(decision_id)
    assert row is not None and row.status == "failed"
    assert row.error is not None and "attempt 1" in row.error and "attempt 2" in row.error
    assert row.cost_tokens == 30  # both attempts' tokens are attributed
    # Retry prompt carries the previous failure back to the model.
    assert "not a valid DecisionPlan" in calls[1]
    assert len(calls) == 2


async def test_runner_marks_failed_on_plan_violation(
    db: SqlitePool, team_id: str, repos: Repos
) -> None:
    repos.team_runtime_repo.ensure(team_id)
    payload = {
        "task_ops": [
            {"op": "create", "task_id": "a", "idempotency_key": "k1"}  # no title
        ]
    }
    runner = _runner(repos, _ok_plan_fn(payload, []))
    decision_id = await runner.run(team_id, trigger_reason="goal.changed")
    row = repos.team_decision_repo.get(decision_id)
    assert row is not None and row.status == "failed"
    assert "title required" in (row.error or "")
    # Plan is preserved for offline review even when rejected.
    assert row.plan is not None and "plan" in row.plan
    # Nothing was applied to the board.
    assert repos.team_task_repo.list_by_team(team_id) == []


async def test_runner_marks_failed_on_crash(
    db: SqlitePool, team_id: str, repos: Repos, monkeypatch: pytest.MonkeyPatch
) -> None:
    repos.team_runtime_repo.ensure(team_id)

    def boom(repos: Any, team_id: str) -> dict[str, Any]:
        raise RuntimeError("snapshot exploded")

    monkeypatch.setattr(decision_mod, "build_decision_snapshot", boom)
    runner = _runner(repos, _ok_plan_fn({"noop": True}, []))
    decision_id = await runner.run(team_id, trigger_reason="goal.changed")
    row = repos.team_decision_repo.get(decision_id)
    assert row is not None and row.status == "failed"
    assert row.error == "runner crashed"
    # Crash path still stamps the cooldown so the team is not retried in a loop.
    runtime = repos.team_runtime_repo.get(team_id)
    assert runtime is not None and runtime.last_decision_at is not None


async def test_runner_skips_paused_or_stopped_teams(
    db: SqlitePool, team_id: str, repos: Repos
) -> None:
    repos.team_runtime_repo.ensure(team_id)
    runner = _runner(repos, _ok_plan_fn({"noop": True}, []))

    repos.team_runtime_repo.update(team_id, expected_version=1, fields={"status": "stopped"})
    assert await runner.run(team_id, trigger_reason="goal.changed") is None
    assert repos.team_decision_repo.list_by_team(team_id) == []

    fresh = repos.team_runtime_repo.get(team_id)
    repos.team_runtime_repo.update(
        team_id,
        expected_version=fresh.version,  # type: ignore[union-attr]
        fields={"status": "idle", "paused_reason": "user pause"},
    )
    assert await runner.run(team_id, trigger_reason="goal.changed") is None
    assert repos.team_decision_repo.list_by_team(team_id) == []


async def test_runner_skips_when_decision_active(
    db: SqlitePool, team_id: str, repos: Repos
) -> None:
    repos.team_runtime_repo.ensure(team_id)
    repos.team_decision_repo.create(team_id, reason="earlier")
    runner = _runner(repos, _ok_plan_fn({"noop": True}, []))
    assert await runner.run(team_id, trigger_reason="goal.changed") is None


async def test_runner_skip_on_cooldown_does_not_create_row(
    db: SqlitePool, team_id: str, repos: Repos
) -> None:
    repos.team_runtime_repo.ensure(team_id)
    repos.team_runtime_repo.update(
        team_id,
        expected_version=1,
        fields={"last_decision_at": int(time.time()) - 10},
    )
    runner = _runner(repos, _ok_plan_fn({"noop": True}, []), cooldown_seconds=300)
    assert await runner.run(team_id, trigger_reason="goal.changed") is None
    assert repos.team_decision_repo.list_by_team(team_id) == []


async def test_runner_cooldown_zero_allows_back_to_back(
    db: SqlitePool, team_id: str, repos: Repos
) -> None:
    repos.team_runtime_repo.ensure(team_id)
    runner = _runner(repos, _ok_plan_fn({"noop": True}, []), cooldown_seconds=0)
    first = await runner.run(team_id, trigger_reason="goal.changed")
    second = await runner.run(team_id, trigger_reason="goal.changed")
    assert first is not None and second is not None


def _coordinator_with_runner(repos: Repos, runner: DecisionRunner | None, gate: Any) -> Any:
    from octop.infra.agents.teams.coordinator import TeamCoordinator

    return TeamCoordinator(
        event_repo=repos.team_event_repo,
        runtime_repo=repos.team_runtime_repo,
        task_repo=repos.team_task_repo,
        decision_runner=runner,
        shadow_gate=gate,
    )


async def test_coordinator_triggers_shadow_decision_on_judgment(
    db: SqlitePool, team_id: str, repos: Repos
) -> None:
    calls: list[str] = []
    runner = _runner(repos, _ok_plan_fn({"noop": True}, calls))
    coord = _coordinator_with_runner(repos, runner, gate=lambda tid: tid == team_id)
    event_id = coord.record(
        team_id,
        type="member.replied",
        dedupe_key="inbox:1",
        payload={"status": "failed"},
    )
    assert event_id is not None
    tasks = list(coord._bg_tasks)
    assert len(tasks) == 1
    await asyncio.gather(*tasks)
    assert len(calls) == 1
    assert "member.replied" in calls[0]  # trigger reason in the prompt
    rows = repos.team_decision_repo.list_by_team(team_id)
    assert len(rows) == 1 and rows[0].trigger_event_ids == [event_id]


async def test_coordinator_gate_off_never_runs(db: SqlitePool, team_id: str, repos: Repos) -> None:
    runner = _runner(repos, _ok_plan_fn({"noop": True}, []))
    coord = _coordinator_with_runner(repos, runner, gate=lambda tid: False)
    coord.record(
        team_id,
        type="goal.changed",
        dedupe_key=f"goal:g1:{time.time_ns()}",
    )
    await asyncio.sleep(0)
    assert not coord._bg_tasks
    assert repos.team_decision_repo.list_by_team(team_id) == []


async def test_coordinator_without_runner_is_inert(
    db: SqlitePool, team_id: str, repos: Repos
) -> None:
    coord = _coordinator_with_runner(repos, None, gate=lambda tid: True)
    assert (
        coord.record(team_id, type="goal.changed", dedupe_key=f"goal:g1:{time.time_ns()}")
        is not None
    )
    await asyncio.sleep(0)
    assert repos.team_decision_repo.list_by_team(team_id) == []


def test_validate_member_roster_restricts_assignee(
    db: SqlitePool, team_id: str, repos: Repos
) -> None:
    _member(db)
    AgentRepo(db).create(agent_id="member-2", user_id=1, name="m2", kind="expert")
    plan = _plan(
        task_ops=[
            {
                "op": "create",
                "task_id": "a",
                "title": "A",
                "assignee": "member-2",
                "idempotency_key": "k1",
            }
        ]
    )
    assert "unknown assignee" in " ".join(
        validate_decision_plan(plan, repos, team_id, member_ids={"member-1"})  # type: ignore[arg-type]
    )
    assert (
        validate_decision_plan(
            plan,
            repos,
            team_id,
            member_ids={"member-1", "member-2"},  # type: ignore[arg-type]
        )
        == []
    )


@pytest.mark.asyncio
async def test_roster_failure_fails_the_decision_closed(
    db: SqlitePool, team_id: str, repos: Repos
) -> None:
    """An unreadable roster must not widen assignee validation.

    Falling back to ``member_ids=None`` silently accepts any agent id in the
    system, so a plan could dispatch this team's work to a stranger. A
    configured-but-unreadable roster fails the decision instead.
    """
    repos.team_runtime_repo.ensure(team_id)
    calls: list[str] = []

    def _broken(_team_id: str) -> set[str]:
        raise RuntimeError("manifest unreadable")

    runner = _runner(
        repos,
        _ok_plan_fn({"noop": True, "rationale": "quiet"}, calls),
        member_ids_fn=_broken,
    )

    decision_id = await runner.run(team_id, trigger_reason="goal.changed")

    assert decision_id is not None
    row = repos.team_decision_repo.get(decision_id)
    assert row is not None
    assert row.status == "failed"
    assert "roster unavailable" in (row.error or "")
