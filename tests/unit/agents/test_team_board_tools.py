"""Unit tests for the built-in team board agent tools (goal/task)."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from langgraph.config import var_child_runnable_config

from octop.infra.agents.teams.board_tools import (
    TEAM_BOARD_TOOL_NAMES,
    build_team_board_tools,
)
from octop.infra.agents.tool_scope import tool_scope
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.goal_deliverables import GoalDeliverableRepo
from octop.infra.db.repos.team_goals import TeamGoalRepo
from octop.infra.db.repos.team_runtime import TeamRuntimeRepo
from octop.infra.db.repos.team_tasks import TeamTaskRepo
from octop.infra.db.repos.team_waits import TeamWaitRepo
from octop.infra.db.repos.users import UserRepo


@contextmanager
def _configurable(**kwargs: object):
    token = var_child_runnable_config.set({"configurable": kwargs})
    try:
        yield
    finally:
        var_child_runnable_config.reset(token)


@pytest.fixture
def db(tmp_path: Path) -> SqlitePool:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return pool


@pytest.fixture
def env(db: SqlitePool) -> dict[str, Any]:
    alice = UserRepo(db).create(username="alice", password_hash="h", role="user")
    bob = UserRepo(db).create(username="bob", password_hash="h", role="user")
    agent_repo = AgentRepo(db)
    team_id = "team-01"
    agent_repo.create(agent_id=team_id, user_id=alice, name="crew", kind="team")
    agent_repo.create(agent_id="member-01", user_id=alice, name="worker")
    return {
        "alice": alice,
        "bob": bob,
        "team_id": team_id,
        "member_id": "member-01",
        "goal_repo": TeamGoalRepo(db),
        "deliverable_repo": GoalDeliverableRepo(db),
        "task_repo": TeamTaskRepo(db),
        "wait_repo": TeamWaitRepo(db),
        "runtime_repo": TeamRuntimeRepo(db),
        "agent_repo": agent_repo,
    }


def _tools(env: dict[str, Any]) -> dict[str, Any]:
    tools = build_team_board_tools(
        goal_repo=env["goal_repo"],
        task_repo=env["task_repo"],
        deliverable_repo=env["deliverable_repo"],
        agent_repo=env["agent_repo"],
        wait_repo=env["wait_repo"],
        runtime_repo=env["runtime_repo"],
    )
    assert [t.name for t in tools] == list(TEAM_BOARD_TOOL_NAMES)
    return {t.name: t for t in tools}


def _tools_with_roster(env: dict[str, Any], roster) -> dict[str, Any]:
    tools = build_team_board_tools(
        goal_repo=env["goal_repo"],
        task_repo=env["task_repo"],
        deliverable_repo=env["deliverable_repo"],
        agent_repo=env["agent_repo"],
        wait_repo=env["wait_repo"],
        runtime_repo=env["runtime_repo"],
        member_ids_fn=roster,
    )
    return {t.name: t for t in tools}


async def _call(env: dict[str, Any], user_id: int, tool: Any, **args: Any) -> dict[str, Any]:
    with tool_scope(env["team_id"], user_id), _configurable():
        raw = await tool.ainvoke(args)
    return json.loads(raw)


async def test_goal_set_replaces_active_goal(db: SqlitePool, env: dict[str, Any]) -> None:
    tools = _tools(env)
    first = await _call(env, env["alice"], tools["goal_set"], title="Ship v1")
    assert first["goal"]["status"] == "active"
    second = await _call(env, env["alice"], tools["goal_set"], title="Ship v2")
    assert second["goal"]["goal_id"] != first["goal"]["goal_id"]
    goals = env["goal_repo"].list_by_team(env["team_id"])
    statuses = {g.goal_id: g.status for g in goals}
    assert statuses[first["goal"]["goal_id"]] == "archived"
    assert statuses[second["goal"]["goal_id"]] == "active"
    runtime = env["runtime_repo"].get(env["team_id"])
    assert runtime is not None and runtime.goal_id == second["goal"]["goal_id"]


async def test_task_lifecycle_and_illegal_transition(env: dict[str, Any]) -> None:
    tools = _tools(env)
    goal = await _call(env, env["alice"], tools["goal_set"], title="G")
    created = await _call(
        env,
        env["alice"],
        tools["task_create"],
        title="T1",
        goal_id=goal["goal"]["goal_id"],
        assignee=env["member_id"],
        priority=3,
    )
    task = created["task"]
    assert task["status"] == "proposed"
    assert task["version"] == 1

    advanced = await _call(
        env, env["alice"], tools["task_update"], task_id=task["task_id"], status="running"
    )
    assert advanced["task"]["status"] == "running"
    assert advanced["task"]["version"] == 2

    done = await _call(
        env,
        env["alice"],
        tools["task_update"],
        task_id=task["task_id"],
        status="done",
        result_summary="shipped",
    )
    assert done["task"]["status"] == "done"

    reopened = await _call(
        env, env["alice"], tools["task_update"], task_id=task["task_id"], status="running"
    )
    assert "error" in reopened

    listed = await _call(env, env["alice"], tools["task_list"])
    assert listed["open_count"] == 0
    assert [t["task_id"] for t in listed["tasks"]] == [task["task_id"]]


async def test_task_create_rejects_unknown_goal_and_assignee(env: dict[str, Any]) -> None:
    tools = _tools(env)
    bad_goal = await _call(env, env["alice"], tools["task_create"], title="T", goal_id="nope")
    assert "error" in bad_goal
    bad_member = await _call(env, env["alice"], tools["task_create"], title="T", assignee="ghost")
    assert "error" in bad_member


async def test_non_creator_cannot_use_board_tools(env: dict[str, Any]) -> None:
    tools = _tools(env)
    denied = await _call(env, env["bob"], tools["task_create"], title="T")
    assert "error" in denied
    assert "creator" in denied["error"]


async def test_task_writes_emit_board_events(env: dict[str, Any]) -> None:
    pushed: list[tuple[int, dict[str, Any]]] = []

    async def _notify(user_id: int, frame: dict[str, Any]) -> None:
        pushed.append((user_id, frame))

    tools = build_team_board_tools(
        goal_repo=env["goal_repo"],
        task_repo=env["task_repo"],
        deliverable_repo=env["deliverable_repo"],
        agent_repo=env["agent_repo"],
        task_notify=_notify,
    )
    by_name = {t.name: t for t in tools}
    creator_id = env["agent_repo"].get(env["team_id"]).user_id

    created = await _call(env, env["alice"], by_name["task_create"], title="T1")
    task = created["task"]
    assert pushed == [
        (
            creator_id,
            {
                "type": "team.task.updated",
                "team_id": env["team_id"],
                "task_id": task["task_id"],
                "status": "proposed",
                "version": 1,
            },
        )
    ]

    await _call(
        env, env["alice"], by_name["task_update"], task_id=task["task_id"], status="running"
    )
    assert pushed[1][1]["status"] == "running"
    assert pushed[1][1]["version"] == 2

    # Reads and rejected writes emit nothing.
    await _call(env, env["alice"], by_name["task_list"])
    await _call(env, env["bob"], by_name["task_create"], title="nope")
    assert len(pushed) == 2


async def test_board_writes_append_shadow_events(db: SqlitePool, env: dict[str, Any]) -> None:
    """Successful writes append shadow-ledger events; reads and rejects don't."""
    from octop.infra.agents.teams import TeamCoordinator  # noqa: PLC0415
    from octop.infra.db.repos.team_events import TeamEventRepo  # noqa: PLC0415
    from octop.infra.db.repos.team_runtime import TeamRuntimeRepo  # noqa: PLC0415

    recorder = TeamCoordinator(
        event_repo=TeamEventRepo(db),
        runtime_repo=TeamRuntimeRepo(db),
    )
    tools = build_team_board_tools(
        goal_repo=env["goal_repo"],
        task_repo=env["task_repo"],
        deliverable_repo=env["deliverable_repo"],
        agent_repo=env["agent_repo"],
        event_recorder=recorder,
    )
    by_name = {t.name: t for t in tools}
    goal = await _call(env, env["alice"], by_name["goal_set"], title="Ship v2")
    task = await _call(
        env,
        env["alice"],
        by_name["task_create"],
        title="Do it",
        goal_id=goal["goal"]["goal_id"],
    )
    running = await _call(
        env,
        env["alice"],
        by_name["task_update"],
        task_id=task["task"]["task_id"],
        status="running",
    )

    events = TeamEventRepo(db).list_after(env["team_id"])
    assert [(e.type, e.actor) for e in events] == [
        ("goal.changed", "host"),
        ("task.changed", "host"),
        ("task.changed", "host"),
    ]
    assert events[1].dedupe_key == f"task:{task['task']['task_id']}:v{task['task']['version']}"
    assert events[2].dedupe_key == f"task:{task['task']['task_id']}:v{running['task']['version']}"
    # The first event lazily provisioned the runtime row.
    assert TeamRuntimeRepo(db).get(env["team_id"]) is not None


@pytest.mark.asyncio
async def test_terminal_update_cancels_open_waits(env: dict[str, Any]) -> None:
    task = env["task_repo"].create(
        team_id=env["team_id"], title="T", assignee=env["member_id"], status="running"
    )
    wait = env["wait_repo"].create(
        env["team_id"],
        kind="member_reply",
        idempotency_key="dispatch:t:1",
        task_id=task.task_id,
        external_ref="job-1",
    )
    assert wait is not None
    tools = _tools(env)

    out = await _call(env, env["alice"], tools["task_update"], task_id=task.task_id, status="done")

    assert out["task"]["status"] == "done"
    assert out["task"]["active_wait_id"] is None
    assert env["wait_repo"].get(wait.wait_id).status == "cancelled"  # type: ignore[union-attr]


async def test_deliverable_update_attaches_file(env: dict[str, Any]) -> None:
    tools = _tools(env)
    goal = await _call(env, env["alice"], tools["goal_set"], title="G")
    deliverable = env["deliverable_repo"].create(
        goal_id=goal["goal"]["goal_id"],
        title="Report",
        created_by=env["alice"],
    )
    result = await _call(
        env,
        env["alice"],
        tools["deliverable_update"],
        deliverable_id=deliverable.deliverable_id,
        status="produced",
        artifact_type="file",
        artifact_ref="reports/plan.docx",
        artifact_agent_id="member-01",
    )
    assert result["status"] == "produced"
    assert result["artifact_agent_id"] == "member-01"


async def test_deliverable_update_rejects_bad_artifact(env: dict[str, Any]) -> None:
    tools = _tools(env)
    goal = await _call(env, env["alice"], tools["goal_set"], title="G")
    deliverable = env["deliverable_repo"].create(
        goal_id=goal["goal"]["goal_id"],
        title="Report",
        created_by=env["alice"],
    )
    result = await _call(
        env,
        env["alice"],
        tools["deliverable_update"],
        deliverable_id=deliverable.deliverable_id,
        status="produced",
    )
    assert "error" in result


async def test_task_create_rejects_assignee_outside_the_roster(
    db: SqlitePool, env: dict[str, Any]
) -> None:
    """A host must not hand its work to an agent outside its own team.

    The decision path validates against the roster; the tool path used to
    check only that the agent existed, so a stranger (or a stranger's team
    member) was accepted and the dispatcher then shipped this team's task
    spec to them.
    """
    stranger = env["agent_repo"].create(agent_id="stranger-01", user_id=env["bob"], name="outsider")
    assert stranger is not None
    tools = _tools_with_roster(env, lambda _tid: {env["member_id"]})

    out = await _call(env, env["alice"], tools["task_create"], title="T", assignee="stranger-01")
    assert "error" in out
    assert "not a member" in out["error"]
    assert env["task_repo"].list_by_team(env["team_id"]) == []


async def test_task_update_rejects_assignee_outside_the_roster(
    db: SqlitePool, env: dict[str, Any]
) -> None:
    env["agent_repo"].create(agent_id="stranger-02", user_id=env["bob"], name="outsider")
    tools = _tools_with_roster(env, lambda _tid: {env["member_id"]})
    created = await _call(
        env, env["alice"], tools["task_create"], title="T", assignee=env["member_id"]
    )
    task_id = created["task"]["task_id"]

    out = await _call(
        env, env["alice"], tools["task_update"], task_id=task_id, assignee="stranger-02"
    )

    assert "error" in out
    assert "not a member" in out["error"]
    assert env["task_repo"].get(task_id).assignee == env["member_id"]  # type: ignore[union-attr]


async def test_task_create_rejects_a_foreign_goal(db: SqlitePool, env: dict[str, Any]) -> None:
    """A task may only hang off its own team's goal."""
    other_team = "team-02"
    env["agent_repo"].create(agent_id=other_team, user_id=env["bob"], name="other", kind="team")
    foreign = env["goal_repo"].create(team_id=other_team, title="theirs", created_by=env["bob"])

    tools = _tools_with_roster(env, lambda _tid: {env["member_id"]})
    out = await _call(env, env["alice"], tools["task_create"], title="T", goal_id=foreign.goal_id)

    assert "error" in out
    assert env["task_repo"].list_by_team(env["team_id"]) == []


async def test_unreadable_roster_blocks_assignment(db: SqlitePool, env: dict[str, Any]) -> None:
    """A roster read failure must not silently widen the check."""

    def _broken(_tid: str) -> set[str]:
        raise RuntimeError("manifest unreadable")

    tools = _tools_with_roster(env, _broken)
    out = await _call(env, env["alice"], tools["task_create"], title="T", assignee=env["member_id"])

    assert "error" in out
    assert "roster" in out["error"].lower()


async def test_autonomous_task_defaults_to_self_when_unassigned(
    db: SqlitePool, env: dict[str, Any]
) -> None:
    """An unassigned solo task would be escalated as ``no assignee`` forever."""
    tools = build_team_board_tools(
        goal_repo=env["goal_repo"],
        task_repo=env["task_repo"],
        deliverable_repo=env["deliverable_repo"],
        agent_repo=env["agent_repo"],
        wait_repo=env["wait_repo"],
        runtime_repo=env["runtime_repo"],
        self_only=True,
    )
    by_name = {t.name: t for t in tools}

    out = await _call(env, env["alice"], by_name["task_create"], title="T")

    assert "error" not in out
    assert out["task"]["assignee"] == env["team_id"]
