"""Unit tests for real dispatch execution (docs/team-drive-v2.md §7.1, phase 3)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from octop.infra.agents.teams.dispatcher import DispatchTarget, TeamDispatcher
from octop.infra.agents.teams.task_state import is_valid_task_transition
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.team_runtime import TeamRuntimeRepo
from octop.infra.db.repos.team_task_deps import TeamTaskDepsRepo
from octop.infra.db.repos.team_tasks import TeamTaskRepo
from octop.infra.db.repos.team_waits import TeamWaitRepo
from octop.infra.db.repos.users import UserRepo

NOW = 1_800_000_000
TARGET = DispatchTarget(
    team_id="team-01",
    user_id=1,
    thread_id="thr_room",
    session_key="team-01:dashboard:1:dm",
)


class Recorder:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.tasks: list[Any] = []

    def record(self, team_id: str | None, **kwargs: Any) -> str:
        self.events.append({"team_id": team_id, **kwargs})
        return "evt"

    def record_task_change(self, row: Any, *, actor: str) -> str:
        self.tasks.append((row, actor))
        return "evt"


class Adapter:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.fail = fail

    async def __call__(self, team_id: str, member_id: str, message: str, target: Any) -> str:
        self.calls.append((team_id, member_id, message, target))
        if self.fail:
            raise RuntimeError("inbox unavailable")
        return "job-1"


@pytest.fixture
def db(tmp_path: Path) -> SqlitePool:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return pool


@pytest.fixture
def repos(db: SqlitePool) -> SimpleNamespace:
    return SimpleNamespace(
        team_task_repo=TeamTaskRepo(db),
        team_task_deps_repo=TeamTaskDepsRepo(db),
        team_wait_repo=TeamWaitRepo(db),
        team_runtime_repo=TeamRuntimeRepo(db),
        agent_repo=AgentRepo(db),
    )


@pytest.fixture
def team_id(db: SqlitePool, repos: SimpleNamespace) -> str:
    user = UserRepo(db).create(username="alice", password_hash="h", role="user")
    repos.agent_repo.create(agent_id="team-01", user_id=user, name="crew", kind="team")
    repos.agent_repo.create(agent_id="member-a", user_id=user, name="a", kind="expert")
    repos.agent_repo.create(agent_id="member-b", user_id=user, name="b", kind="expert")
    return "team-01"


def _autopilot(repos: SimpleNamespace, team_id: str, *, status: str = "running") -> None:
    row = repos.team_runtime_repo.ensure(team_id)
    repos.team_runtime_repo.update(
        team_id,
        expected_version=row.version,
        fields={"status": status, "mode": "autopilot"},
    )


def _dispatcher(
    repos: SimpleNamespace,
    adapter: Adapter | None = None,
) -> tuple[TeamDispatcher, Adapter, Recorder]:
    adapter = adapter or Adapter()
    recorder = Recorder()

    async def resolve(_team_id: str) -> DispatchTarget:
        return TARGET

    dispatcher = TeamDispatcher(
        repos=repos,
        resolve_target=resolve,
        submit_peer=adapter,
        recorder=recorder,
        clock=lambda: NOW,
    )
    return dispatcher, adapter, recorder


@pytest.mark.asyncio
async def test_dispatch_creates_wait_and_marks_task_running(
    repos: SimpleNamespace, team_id: str
) -> None:
    _autopilot(repos, team_id)
    task = repos.team_task_repo.create(
        team_id=team_id, title="T", spec="do it", assignee="member-a"
    )
    dispatcher, adapter, _recorder = _dispatcher(repos)

    outcomes = await dispatcher.dispatch_ready(team_id, now=NOW)

    assert [o.status for o in outcomes] == ["dispatched"]
    assert adapter.calls == [(team_id, "member-a", "do it", TARGET)]
    updated = repos.team_task_repo.get(task.task_id)
    assert updated is not None and updated.status == "running"
    assert updated.active_wait_id
    wait = repos.team_wait_repo.get(updated.active_wait_id)
    assert wait is not None and wait.kind == "member_reply"
    assert wait.external_ref == "job-1"
    assert wait.task_id == task.task_id
    runtime = repos.team_runtime_repo.get(team_id)
    assert runtime is not None and runtime.status == "waiting"


@pytest.mark.asyncio
async def test_dispatch_without_assignee_escalates(repos: SimpleNamespace, team_id: str) -> None:
    _autopilot(repos, team_id)
    task = repos.team_task_repo.create(team_id=team_id, title="T")
    dispatcher, adapter, recorder = _dispatcher(repos)

    outcomes = await dispatcher.dispatch_ready(team_id, now=NOW)

    assert [o.status for o in outcomes] == ["escalated"]
    assert adapter.calls == []
    updated = repos.team_task_repo.get(task.task_id)
    assert updated is not None and updated.status == "blocked"
    assert any(e["type"] == "wait.timeout" for e in recorder.events)
    assert repos.team_wait_repo.list_open_for_team(team_id) == []


@pytest.mark.asyncio
async def test_paused_runtime_does_not_dispatch(repos: SimpleNamespace, team_id: str) -> None:
    _autopilot(repos, team_id, status="paused_user")
    repos.team_task_repo.create(team_id=team_id, title="T", assignee="member-a")
    dispatcher, adapter, _recorder = _dispatcher(repos)

    assert await dispatcher.dispatch_ready(team_id, now=NOW) == []
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_submit_failure_parks_task_and_settles_wait(
    repos: SimpleNamespace, team_id: str
) -> None:
    """A failed dispatch parks the task for re-planning, it does not kill it.

    ``failed`` is terminal, so the decision unit would never touch the task
    again — that defeats the escalation event recorded alongside it. It is
    also not a legal edge from ``assigned``: writing it left the board
    rejecting the server's own write on the next PATCH.
    """
    _autopilot(repos, team_id)
    task = repos.team_task_repo.create(team_id=team_id, title="T", assignee="member-a")
    dispatcher, _adapter, recorder = _dispatcher(repos, Adapter(fail=True))

    outcomes = await dispatcher.dispatch_ready(team_id, now=NOW)

    assert [o.status for o in outcomes] == ["failed"]
    updated = repos.team_task_repo.get(task.task_id)
    assert updated is not None
    assert updated.status == "blocked"
    assert "inbox unavailable" in updated.blocked_reason
    # INV-2: whatever the dispatcher writes must be reachable from the state
    # it came from, or the board rejects the server's own transition.
    assert is_valid_task_transition("assigned", updated.status)
    waits = repos.team_wait_repo.list_by_team(team_id)
    assert [w.status for w in waits] == ["timed_out"]
    assert any(e["type"] == "wait.timeout" for e in recorder.events)


@pytest.mark.asyncio
async def test_window_before_start_opens_timer_wait(repos: SimpleNamespace, team_id: str) -> None:
    _autopilot(repos, team_id)
    task = repos.team_task_repo.create(team_id=team_id, title="T", assignee="member-a")
    repos.team_task_repo.update(
        task.task_id, expected_version=task.version, fields={"window_start": NOW + 120}
    )
    dispatcher, adapter, _recorder = _dispatcher(repos)

    outcomes = await dispatcher.dispatch_ready(team_id, now=NOW)

    assert [o.action for o in outcomes] == ["wait_window"]
    assert adapter.calls == []
    updated = repos.team_task_repo.get(task.task_id)
    assert updated is not None and updated.active_wait_id
    wait = repos.team_wait_repo.get(updated.active_wait_id)
    assert wait is not None and wait.kind == "timer" and wait.due_at == NOW + 120


@pytest.mark.asyncio
async def test_past_deadline_blocks_and_escalates(repos: SimpleNamespace, team_id: str) -> None:
    _autopilot(repos, team_id)
    task = repos.team_task_repo.create(team_id=team_id, title="T", assignee="member-a")
    repos.team_task_repo.update(
        task.task_id, expected_version=task.version, fields={"due_at": NOW - 10}
    )
    dispatcher, adapter, recorder = _dispatcher(repos)

    outcomes = await dispatcher.dispatch_ready(team_id, now=NOW)

    assert [o.action for o in outcomes] == ["escalate_deadline"]
    assert adapter.calls == []
    updated = repos.team_task_repo.get(task.task_id)
    assert updated is not None and updated.status == "blocked"
    assert any(e["type"] == "wait.timeout" for e in recorder.events)


@pytest.mark.asyncio
async def test_active_wait_skips_task(repos: SimpleNamespace, team_id: str) -> None:
    _autopilot(repos, team_id)
    task = repos.team_task_repo.create(team_id=team_id, title="T", assignee="member-a")
    wait = repos.team_wait_repo.create(
        team_id, kind="timer", idempotency_key="timer:x", task_id=task.task_id, due_at=NOW + 60
    )
    assert wait is not None
    repos.team_task_repo.update(
        task.task_id, expected_version=task.version, fields={"active_wait_id": wait.wait_id}
    )
    dispatcher, adapter, _recorder = _dispatcher(repos)

    outcomes = await dispatcher.dispatch_ready(team_id, now=NOW)

    assert [o.action for o in outcomes] == ["skip_wait"]
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_concurrency_key_serializes_member(repos: SimpleNamespace, team_id: str) -> None:
    _autopilot(repos, team_id)
    repos.team_task_repo.create(
        team_id=team_id, title="busy", assignee="member-a", status="running", priority=9
    )
    waiting = repos.team_task_repo.create(team_id=team_id, title="next", assignee="member-a")
    other = repos.team_task_repo.create(team_id=team_id, title="other", assignee="member-b")
    dispatcher, adapter, _recorder = _dispatcher(repos)

    await dispatcher.dispatch_ready(team_id, now=NOW)

    assert [call[1] for call in adapter.calls] == ["member-b"]
    assert repos.team_task_repo.get(waiting.task_id).status == "proposed"  # type: ignore[union-attr]
    assert repos.team_task_repo.get(other.task_id).status == "running"  # type: ignore[union-attr]
