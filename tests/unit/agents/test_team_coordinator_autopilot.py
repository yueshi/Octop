"""Authoritative coordinator routing and wait settlement (phase 3)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from octop.infra.agents.teams.coordinator import TeamCoordinator
from octop.infra.agents.teams.decision import DecisionRunner
from octop.infra.agents.teams.dispatcher import DispatchTarget, TeamDispatcher
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


class Runner:
    def __init__(self, *, decision_id: str | None = "d1") -> None:
        self.calls: list[dict[str, Any]] = []
        self.decision_id = decision_id

    async def run(self, team_id: str, **kwargs: Any) -> str | None:
        self.calls.append({"team_id": team_id, **kwargs})
        return self.decision_id


class Dispatcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def dispatch_ready(self, team_id: str, **kwargs: Any) -> list[Any]:
        self.calls.append(team_id)
        return []


@pytest.fixture
def db(tmp_path: Path) -> SqlitePool:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return pool


@pytest.fixture
def repos(db: SqlitePool) -> SimpleNamespace:
    return SimpleNamespace(
        team_event_repo=TeamEventRepo(db),
        team_runtime_repo=TeamRuntimeRepo(db),
        team_task_repo=TeamTaskRepo(db),
        team_wait_repo=TeamWaitRepo(db),
        team_goal_repo=TeamGoalRepo(db),
        team_task_deps_repo=TeamTaskDepsRepo(db),
        team_decision_repo=TeamDecisionRepo(db),
        agent_repo=AgentRepo(db),
        usage_repo=UsageRepo(db),
    )


@pytest.fixture
def team_id(db: SqlitePool, repos: SimpleNamespace) -> str:
    user = UserRepo(db).create(username="alice", password_hash="h", role="user")
    repos.agent_repo.create(agent_id="team-01", user_id=user, name="crew", kind="team")
    repos.agent_repo.create(agent_id="member-1", user_id=user, name="m1", kind="expert")
    return "team-01"


def _autopilot(repos: SimpleNamespace, team_id: str, *, status: str = "running") -> None:
    row = repos.team_runtime_repo.ensure(team_id)
    repos.team_runtime_repo.update(
        team_id,
        expected_version=row.version,
        fields={"status": status, "mode": "autopilot"},
    )


def _coordinator(
    repos: SimpleNamespace,
    *,
    runner: Runner | None = None,
    dispatcher: Dispatcher | None = None,
) -> TeamCoordinator:
    return TeamCoordinator(
        event_repo=repos.team_event_repo,
        runtime_repo=repos.team_runtime_repo,
        task_repo=repos.team_task_repo,
        wait_repo=repos.team_wait_repo,
        decision_runner=runner,
        dispatcher=dispatcher,
        apply_plans=True,
    )


async def test_routine_event_dispatches_and_is_consumed(
    repos: SimpleNamespace, team_id: str
) -> None:
    _autopilot(repos, team_id)
    runner = Runner()
    dispatcher = Dispatcher()
    coordinator = _coordinator(repos, runner=runner, dispatcher=dispatcher)
    event_id = coordinator.record(
        team_id, type="task.changed", dedupe_key="task:1:v1", actor="user:1"
    )
    await coordinator.route_events(
        team_id, [event_id or ""], need_decision=False, reason="task.changed"
    )

    assert runner.calls == []
    assert dispatcher.calls == [team_id]
    event = repos.team_event_repo.get(event_id or "")
    assert event is not None and event.consumed_at is not None


async def test_judgment_event_runs_decision_then_dispatch(
    repos: SimpleNamespace, team_id: str
) -> None:
    _autopilot(repos, team_id)
    runner = Runner()
    dispatcher = Dispatcher()
    coordinator = _coordinator(repos, runner=runner, dispatcher=dispatcher)
    event_id = coordinator.record(
        team_id, type="goal.changed", dedupe_key="goal:g1:1", actor="user:1"
    )
    await coordinator.route_events(
        team_id, [event_id or ""], need_decision=True, reason="goal.changed"
    )

    assert len(runner.calls) == 1
    assert runner.calls[0]["apply_plan"] is True
    assert dispatcher.calls == [team_id]
    event = repos.team_event_repo.get(event_id or "")
    assert event is not None and event.decision_id == "d1"


async def test_self_event_is_consumed_without_decision(
    repos: SimpleNamespace, team_id: str
) -> None:
    _autopilot(repos, team_id)
    runner = Runner()
    dispatcher = Dispatcher()
    coordinator = _coordinator(repos, runner=runner, dispatcher=dispatcher)
    event_id = coordinator.record(
        team_id, type="goal.changed", dedupe_key="goal:g1:1", actor="host:d9"
    )
    assert event_id is not None
    event = repos.team_event_repo.get(event_id)
    assert event is not None and event.consumed_at is not None
    assert runner.calls == []


async def test_decision_skip_keeps_events_and_restores_status(
    repos: SimpleNamespace, team_id: str
) -> None:
    _autopilot(repos, team_id)
    runner = Runner(decision_id=None)
    dispatcher = Dispatcher()
    coordinator = _coordinator(repos, runner=runner, dispatcher=dispatcher)
    event_id = coordinator.record(
        team_id, type="goal.changed", dedupe_key="goal:g1:1", actor="user:1"
    )
    await coordinator.route_events(
        team_id, [event_id or ""], need_decision=True, reason="goal.changed"
    )

    event = repos.team_event_repo.get(event_id or "")
    assert event is not None and event.consumed_at is None
    runtime = repos.team_runtime_repo.get(team_id)
    assert runtime is not None and runtime.status == "running"
    assert dispatcher.calls == []


async def test_member_reply_settles_wait_and_task(repos: SimpleNamespace, team_id: str) -> None:
    coordinator = _coordinator(repos)
    task = repos.team_task_repo.create(
        team_id=team_id, title="T", assignee="member-1", status="running"
    )
    wait = repos.team_wait_repo.create(
        team_id,
        kind="member_reply",
        idempotency_key="dispatch:t:1",
        task_id=task.task_id,
        external_ref="job-1",
    )
    assert wait is not None
    repos.team_task_repo.update(
        task.task_id, expected_version=task.version, fields={"active_wait_id": wait.wait_id}
    )

    coordinator.handle_member_reply(
        SimpleNamespace(
            inbox_id="job-1",
            status="done",
            reply_text="all good",
            target_agent_id="member-1",
        )
    )

    updated = repos.team_task_repo.get(task.task_id)
    assert updated is not None and updated.status == "done"
    assert updated.result_summary == "all good"
    assert updated.active_wait_id is None
    assert repos.team_wait_repo.get(wait.wait_id).status == "settled"  # type: ignore[union-attr]


async def test_late_reply_closes_timed_out_task(repos: SimpleNamespace, team_id: str) -> None:
    coordinator = _coordinator(repos)
    task = repos.team_task_repo.create(
        team_id=team_id, title="T", assignee="member-1", status="running"
    )
    wait = repos.team_wait_repo.create(
        team_id,
        kind="member_reply",
        idempotency_key="dispatch:t:1",
        task_id=task.task_id,
        external_ref="job-2",
    )
    assert wait is not None
    repos.team_wait_repo.settle(wait.wait_id, status="timed_out")
    repos.team_task_repo.update(
        task.task_id,
        expected_version=task.version,
        fields={"status": "blocked", "blocked_reason": "wait timed out"},
    )

    coordinator.handle_member_reply(
        SimpleNamespace(
            inbox_id="job-2",
            status="done",
            reply_text="finally",
            target_agent_id="member-1",
        )
    )

    updated = repos.team_task_repo.get(task.task_id)
    assert updated is not None and updated.status == "done"
    assert updated.result_summary == "finally"


class PeerAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def submit(self, team_id: str, member_id: str, message: str, target: Any) -> str:
        self.calls.append((team_id, member_id, message, target))
        return "job-e2e"


async def test_event_to_decision_to_dispatch(repos: SimpleNamespace, team_id: str) -> None:
    _autopilot(repos, team_id)
    adapter = PeerAdapter()

    async def resolve(_team_id: str) -> DispatchTarget:
        return DispatchTarget(
            team_id=team_id,
            user_id=1,
            thread_id="thr_room",
            session_key="team-01:dashboard:1:dm",
        )

    dispatcher = TeamDispatcher(
        repos=repos,
        resolve_target=resolve,
        submit_peer=adapter.submit,
        clock=lambda: 1_800_000_000,
    )

    async def plan_fn(prompt: str, team_id: str) -> tuple[str, dict[str, int]]:
        return (
            json.dumps(
                {
                    "task_ops": [
                        {
                            "op": "create",
                            "task_id": "a",
                            "title": "A",
                            "assignee": "member-1",
                            "idempotency_key": "k1",
                        }
                    ]
                }
            ),
            {"total_tokens": 10},
        )

    coordinator = TeamCoordinator(
        event_repo=repos.team_event_repo,
        runtime_repo=repos.team_runtime_repo,
        task_repo=repos.team_task_repo,
        wait_repo=repos.team_wait_repo,
        dispatcher=dispatcher,
        apply_plans=True,
    )
    runner = DecisionRunner(
        repos=repos,
        plan_fn=plan_fn,
        event_recorder=coordinator,
        cooldown_seconds=0,
    )
    coordinator.bind_decision_runner(runner)

    event_id = coordinator.record(
        team_id, type="goal.changed", dedupe_key="goal:g1:1", actor="user:1"
    )
    await coordinator.route_events(
        team_id, [event_id or ""], need_decision=True, reason="goal.changed"
    )

    decisions = repos.team_decision_repo.list_by_team(team_id)
    assert [d.status for d in decisions] == ["committed"]
    tasks = repos.team_task_repo.list_by_team(team_id)
    assert len(tasks) == 1 and tasks[0].assignee == "member-1"
    assert adapter.calls and adapter.calls[0][1] == "member-1"
    wait = repos.team_wait_repo.get_open_by_external_ref("job-e2e", kind="member_reply")
    assert wait is not None and wait.task_id == tasks[0].task_id


async def test_approval_pending_and_resolution(repos: SimpleNamespace, team_id: str) -> None:
    coordinator = _coordinator(repos)
    task = repos.team_task_repo.create(
        team_id=team_id, title="T", assignee="member-1", status="running"
    )

    record = SimpleNamespace(
        pending_id="p1",
        task_id=task.task_id,
        agent_id="member-1",
        thread_id="thr",
    )
    coordinator.record_approval_pending(record)

    updated = repos.team_task_repo.get(task.task_id)
    assert updated is not None and updated.status == "needs_input"
    pair = repos.team_wait_repo.list_open_for_task(task.task_id, kind="approval")
    assert len(pair) == 1

    coordinator.record_hitl_resolved(
        task_id=task.task_id, pending_id="p1", status="approved", agent_id="member-1"
    )

    updated = repos.team_task_repo.get(task.task_id)
    assert updated is not None and updated.status == "running"
    assert updated.active_wait_id is None
    assert repos.team_wait_repo.get(pair[0].wait_id).status == "settled"  # type: ignore[union-attr]


async def test_event_storm_yields_one_decision(repos: SimpleNamespace, team_id: str) -> None:
    _autopilot(repos, team_id)
    runner = Runner()
    dispatcher = Dispatcher()
    coordinator = _coordinator(repos, runner=runner, dispatcher=dispatcher)
    first = coordinator.record(team_id, type="goal.changed", dedupe_key="goal:g1:1", actor="user:1")
    second = coordinator.record(
        team_id,
        type="task.changed",
        dedupe_key="task:t1:v1",
        actor="user:1",
        payload={"status": "blocked"},
    )

    await coordinator.route_events(
        team_id, [first or ""], need_decision=True, reason="goal.changed"
    )

    assert len(runner.calls) == 1
    assert runner.calls[0]["trigger_event_ids"] == [first, second]
    for event_id in (first, second):
        event = repos.team_event_repo.get(event_id or "")
        assert event is not None and event.decision_id == "d1"


def test_task_for_inbox_links_member_pause(repos: SimpleNamespace, team_id: str) -> None:
    task = repos.team_task_repo.create(team_id=team_id, title="T")
    wait = repos.team_wait_repo.create(
        team_id,
        kind="member_reply",
        idempotency_key="dispatch:1",
        task_id=task.task_id,
        external_ref="job-9",
    )
    assert wait is not None
    coordinator = _coordinator(repos)
    assert coordinator.task_for_inbox("job-9") == task.task_id
    assert coordinator.task_for_inbox("missing") is None


@pytest.mark.asyncio
async def test_solo_gate_skips_the_decision_unit(
    db: SqlitePool, team_id: str, repos: SimpleNamespace
) -> None:
    """A digital employee decides inside its own turn.

    Running the LLM decision pass for a single agent only adds cost and
    latency between the wake and the work — there is nobody to plan for.
    """
    from octop.infra.agents.teams.coordinator import TeamCoordinator

    repos.team_runtime_repo.ensure(team_id)
    repos.team_runtime_repo.update(
        team_id,
        expected_version=repos.team_runtime_repo.get(team_id).version,
        fields={"mode": "autopilot", "status": "running"},
    )
    runner = MagicMock()
    runner.run = AsyncMock(return_value="dec-1")
    dispatcher = MagicMock()
    dispatcher.dispatch_ready = AsyncMock(return_value=[])
    coord = TeamCoordinator(
        event_repo=repos.team_event_repo,
        runtime_repo=repos.team_runtime_repo,
        task_repo=repos.team_task_repo,
        wait_repo=repos.team_wait_repo,
        decision_runner=runner,
        dispatcher=dispatcher,
        apply_plans=True,
        solo_gate=lambda _tid: True,
    )

    coord.record(
        team_id,
        type="member.replied",
        dedupe_key="member:solo:1",
        actor="member:solo",
        payload={"status": "blocked"},
        route=False,
    )
    await coord.route_events(team_id, [], need_decision=True, reason="member.replied")

    runner.run.assert_not_called()
