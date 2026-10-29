"""Unit tests for the team autopilot runtime controls (phase 3)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from octop.infra.agents.teams.runtime import HEARTBEAT_FLOOR_SEC, TeamAutopilot
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.team_events import TeamEventRepo
from octop.infra.db.repos.team_goals import TeamGoalRepo
from octop.infra.db.repos.team_runtime import TeamRuntimeRepo
from octop.infra.db.repos.team_tasks import TeamTaskRepo
from octop.infra.db.repos.team_waits import TeamWaitRepo
from octop.infra.db.repos.users import UserRepo

NOW = 1_800_000_000


class Coordinator:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.kicks: list[str] = []
        self.goals: list[Any] = []

    def record(self, team_id: str | None, **kwargs: Any) -> str:
        self.events.append({"team_id": team_id, **kwargs})
        return "evt"

    def kick(self, team_id: str, *, reason: str, need_decision: bool = True) -> None:
        self.kicks.append(reason)

    def record_goal_change(self, row: Any, *, actor: str) -> str:
        self.goals.append(row)
        return "evt"


class CronRepo:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def list_all(self, *, include_disabled: bool = False) -> list[Any]:
        return list(self.rows)


class CronManager:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.deleted: list[str] = []

    def list_by_agent(self, team_id: str) -> list[Any]:
        return [r for r in self.rows if r.agent_id == team_id]

    async def delete(self, cron_id: str) -> None:
        self.deleted.append(cron_id)
        self.rows = [r for r in self.rows if r.cron_id != cron_id]


class AgentManager:
    def __init__(self, cron_manager: Any) -> None:
        self.cron_manager = cron_manager

    def team_inbox(self) -> None:
        return None


class Threads:
    def __init__(self, threads: list[Any] | None = None) -> None:
        self._threads = threads or []
        self.created: list[str] = []

    def list_threads(self, *, agent_id: str, user_id: int, limit: int = 50) -> list[Any]:
        return list(self._threads)

    async def get_or_create_by_key(self, *, session_key: str, **kwargs: Any) -> str:
        self.created.append(session_key)
        return "thr_created"


@pytest.fixture
def db(tmp_path: Path) -> SqlitePool:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return pool


@pytest.fixture
def repos(db: SqlitePool) -> SimpleNamespace:
    return SimpleNamespace(
        team_runtime_repo=TeamRuntimeRepo(db),
        team_goal_repo=TeamGoalRepo(db),
        team_task_repo=TeamTaskRepo(db),
        team_wait_repo=TeamWaitRepo(db),
        team_event_repo=TeamEventRepo(db),
        team_decision_repo=SimpleNamespace(get_active=lambda team: None),
        agent_repo=AgentRepo(db),
        cron_repo=CronRepo([]),
    )


@pytest.fixture
def team_id(db: SqlitePool, repos: SimpleNamespace) -> str:
    user = UserRepo(db).create(username="alice", password_hash="h", role="user")
    repos.agent_repo.create(agent_id="team-01", user_id=user, name="crew", kind="team")
    return "team-01"


def _autopilot(
    repos: SimpleNamespace,
    *,
    cron_manager: CronManager | None = None,
    threads: Threads | None = None,
) -> tuple[TeamAutopilot, Coordinator]:
    manager = AgentManager(cron_manager or CronManager([]))
    autopilot = TeamAutopilot(
        repos=repos,
        agent_manager=manager,
        thread_registry=threads or Threads(),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    coordinator = Coordinator()
    autopilot.bind_coordinator(coordinator)
    return autopilot, coordinator


@pytest.mark.asyncio
async def test_start_sets_running_and_kicks_decision(repos: SimpleNamespace, team_id: str) -> None:
    repos.team_goal_repo.create(
        team_id=team_id, title="G", created_by=1, description="", success_criteria=""
    )
    autopilot, coordinator = _autopilot(repos)

    await autopilot.start(team_id, user_id=1, heartbeat_interval_sec=120)

    runtime = repos.team_runtime_repo.get(team_id)
    assert runtime is not None
    assert runtime.status == "running" and runtime.mode == "autopilot"
    assert runtime.heartbeat_interval_sec == HEARTBEAT_FLOOR_SEC
    assert runtime.next_heartbeat_at == NOW + HEARTBEAT_FLOOR_SEC
    assert coordinator.kicks == ["drive.start"]
    assert any(e["type"] == "runtime.boot" for e in coordinator.events)


@pytest.mark.asyncio
async def test_pause_resume_finish_stop(repos: SimpleNamespace, team_id: str) -> None:
    goal = repos.team_goal_repo.create(
        team_id=team_id, title="G", created_by=1, description="", success_criteria=""
    )
    autopilot, coordinator = _autopilot(repos)
    await autopilot.start(team_id, user_id=1)
    wait = repos.team_wait_repo.create(
        team_id, kind="timer", idempotency_key="timer:1", due_at=NOW + 60
    )
    assert wait is not None

    await autopilot.pause(team_id, reason="lunch")
    runtime = repos.team_runtime_repo.get(team_id)
    assert runtime is not None and runtime.status == "paused_user"

    repos.team_wait_repo.settle(wait.wait_id, status="deferred")
    await autopilot.resume(team_id)
    runtime = repos.team_runtime_repo.get(team_id)
    assert runtime is not None and runtime.status == "running"
    assert repos.team_wait_repo.get(wait.wait_id).status == "waiting"  # type: ignore[union-attr]
    assert coordinator.kicks[-1] == "drive.resume"

    await autopilot.finish(team_id)
    runtime = repos.team_runtime_repo.get(team_id)
    assert runtime is not None and runtime.status == "finishing"
    assert repos.team_goal_repo.get(goal.goal_id).status == "done"  # type: ignore[union-attr]

    task = repos.team_task_repo.create(team_id=team_id, title="T")
    counts = await autopilot.stop(team_id)
    runtime = repos.team_runtime_repo.get(team_id)
    assert runtime is not None and runtime.status == "stopped"
    assert counts["cancelled_tasks"] == 1
    assert counts["cancelled_waits"] == 1
    assert repos.team_task_repo.get(task.task_id).status == "cancelled"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_migrate_legacy_drive_job(repos: SimpleNamespace, team_id: str) -> None:
    row = SimpleNamespace(
        cron_id="drive-team-01",
        agent_id=team_id,
        enabled=True,
        trigger="interval:3600",
        meta={"kind": "team_drive"},
    )
    cron_manager = CronManager([row])
    repos.cron_repo = CronRepo([row])
    autopilot, coordinator = _autopilot(repos, cron_manager=cron_manager)

    migrated = await autopilot.migrate_legacy_drive_jobs(cron_manager)

    assert migrated == 1
    assert cron_manager.deleted == ["drive-team-01"]
    runtime = repos.team_runtime_repo.get(team_id)
    assert runtime is not None
    assert runtime.status == "running" and runtime.mode == "autopilot"
    assert runtime.heartbeat_interval_sec == 3600
    assert any(e["type"] == "runtime.boot" for e in coordinator.events)
    assert coordinator.kicks == []


@pytest.mark.asyncio
async def test_migrate_date_trigger_opens_timer(repos: SimpleNamespace, team_id: str) -> None:
    row = SimpleNamespace(
        cron_id="drive-team-01",
        agent_id=team_id,
        enabled=True,
        trigger="date:2030-01-01T00:00:00",
        meta={"kind": "team_drive"},
    )
    cron_manager = CronManager([row])
    repos.cron_repo = CronRepo([row])
    autopilot, _coordinator = _autopilot(repos, cron_manager=cron_manager)

    await autopilot.migrate_legacy_drive_jobs(cron_manager)

    waits = repos.team_wait_repo.list_open_for_team(team_id)
    assert len(waits) == 1 and waits[0].kind == "timer"


@pytest.mark.asyncio
async def test_resolve_target_prefers_recent_room(repos: SimpleNamespace, team_id: str) -> None:
    thread = SimpleNamespace(thread_id="thr_room", session_key="team-01:dashboard:1:dm")
    autopilot, _coordinator = _autopilot(repos, threads=Threads([thread]))

    target = await autopilot.resolve_target(team_id)

    assert target is not None and target.thread_id == "thr_room"
    assert target.session_key == "team-01:dashboard:1:dm"


@pytest.mark.asyncio
async def test_resolve_target_falls_back_to_dashboard(repos: SimpleNamespace, team_id: str) -> None:
    threads = Threads([])
    autopilot, _coordinator = _autopilot(repos, threads=threads)

    target = await autopilot.resolve_target(team_id)

    assert target is not None and target.thread_id == "thr_created"
    assert threads.created == ["team-01:dashboard:1:dm"]


def test_status_payload_includes_waits(repos: SimpleNamespace, team_id: str) -> None:
    autopilot, _coordinator = _autopilot(repos)
    repos.team_wait_repo.create(team_id, kind="timer", idempotency_key="timer:1", due_at=NOW + 60)
    payload = autopilot.status_payload(team_id)
    assert payload["status"] == "idle"
    assert len(payload["open_waits"]) == 1
    assert payload["open_waits"][0]["kind"] == "timer"


@pytest.mark.asyncio
async def test_migrate_parks_inflight_without_a_wait(repos: SimpleNamespace, team_id: str) -> None:
    repos.agent_repo.create(agent_id="member-1", user_id=1, name="m1", kind="expert")
    orphan = repos.team_task_repo.create(
        team_id=team_id, title="orphan", assignee="member-1", status="running"
    )
    live = repos.team_task_repo.create(
        team_id=team_id, title="live", assignee="member-1", status="running"
    )
    wait = repos.team_wait_repo.create(
        team_id,
        kind="member_reply",
        idempotency_key="dispatch:live:1",
        task_id=live.task_id,
        external_ref="job-live",
    )
    assert wait is not None
    row = SimpleNamespace(
        cron_id="drive-team-01",
        agent_id=team_id,
        enabled=True,
        trigger="interval:3600",
        meta={"kind": "team_drive"},
    )
    cron_manager = CronManager([row])
    repos.cron_repo = CronRepo([row])
    autopilot, _coordinator = _autopilot(repos, cron_manager=cron_manager)

    await autopilot.migrate_legacy_drive_jobs(cron_manager)

    parked = repos.team_task_repo.get(orphan.task_id)
    assert parked is not None
    assert parked.status == "blocked"
    assert "migrated" in parked.blocked_reason
    still_running = repos.team_task_repo.get(live.task_id)
    assert still_running is not None and still_running.status == "running"
