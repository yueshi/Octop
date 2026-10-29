"""Digital-employee board-tool wiring (docs/digital-employee-plan.md S1.3/S1.4).

An autonomous expert owns a goal/task board of its own. It must not be able
to hand a task to another agent: there is no dispatch semantics (no wait
ledger, no callback) for a task the team never dispatched.
"""

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


def _env(db: SqlitePool) -> dict[str, Any]:
    alice = UserRepo(db).create(username="alice", password_hash="h", role="user")
    agent_repo = AgentRepo(db)
    agent_repo.create(agent_id="solo", user_id=alice, name="solo", kind="expert")
    agent_repo.create(agent_id="other", user_id=alice, name="other", kind="expert")
    return {
        "alice": alice,
        "agent_id": "solo",
        "other_id": "other",
        "goal_repo": TeamGoalRepo(db),
        "deliverable_repo": GoalDeliverableRepo(db),
        "task_repo": TeamTaskRepo(db),
        "wait_repo": TeamWaitRepo(db),
        "runtime_repo": TeamRuntimeRepo(db),
        "agent_repo": agent_repo,
    }


def _tools(env: dict[str, Any], *, self_only: bool) -> dict[str, Any]:
    tools = build_team_board_tools(
        goal_repo=env["goal_repo"],
        task_repo=env["task_repo"],
        deliverable_repo=env["deliverable_repo"],
        agent_repo=env["agent_repo"],
        wait_repo=env["wait_repo"],
        runtime_repo=env["runtime_repo"],
        self_only=self_only,
    )
    assert [t.name for t in tools] == list(TEAM_BOARD_TOOL_NAMES)
    return {t.name: t for t in tools}


async def _call(env: dict[str, Any], tool: Any, **args: Any) -> dict[str, Any]:
    with tool_scope(env["agent_id"], env["alice"]), _configurable():
        raw = await tool.ainvoke(args)
    return json.loads(raw)


async def test_self_only_rejects_assigning_to_another_agent(db: SqlitePool) -> None:
    env = _env(db)
    tools = _tools(env, self_only=True)
    payload = await _call(env, tools["task_create"], title="T", assignee="other")
    assert "error" in payload
    assert "themselves" in payload["error"]


async def test_self_only_allows_assigning_to_self(db: SqlitePool) -> None:
    env = _env(db)
    tools = _tools(env, self_only=True)
    payload = await _call(env, tools["task_create"], title="T", assignee="solo")
    assert payload["task"]["assignee"] == "solo"


async def test_self_only_blocks_reassigning_away(db: SqlitePool) -> None:
    env = _env(db)
    tools = _tools(env, self_only=True)
    created = await _call(env, tools["task_create"], title="T", assignee="solo")
    payload = await _call(
        env,
        tools["task_update"],
        task_id=created["task"]["task_id"],
        assignee="other",
    )
    assert "error" in payload
    assert "themselves" in payload["error"]


async def test_team_host_may_still_assign_members(db: SqlitePool) -> None:
    env = _env(db)
    tools = _tools(env, self_only=False)
    payload = await _call(env, tools["task_create"], title="T", assignee="other")
    assert payload["task"]["assignee"] == "other"
