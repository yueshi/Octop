"""Unit tests for the global team sweeper (docs/team-drive-v2.md §5.4)."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from octop.infra.agents.teams.sweeper import TeamSweeper
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.autonomy_policy import AutonomyPolicyRepo
from octop.infra.db.repos.team_decisions import TeamDecisionRepo
from octop.infra.db.repos.team_events import TeamEventRepo
from octop.infra.db.repos.team_runtime import TeamRuntimeRepo
from octop.infra.db.repos.team_tasks import TeamTaskRepo
from octop.infra.db.repos.team_waits import TeamWaitRepo
from octop.infra.db.repos.users import UserRepo


class Coordinator:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.kicks: list[tuple[str, str]] = []
        self.expired: list[str] = []

    def record(self, team_id: str | None, **kwargs: Any) -> str:
        self.events.append({"team_id": team_id, **kwargs})
        return "evt"

    def record_heartbeat(self, team_id: str, *, bucket: int) -> str:
        self.events.append({"team_id": team_id, "type": "sweeper.heartbeat", "bucket": bucket})
        return "evt"

    def record_task_change(self, row: Any, *, actor: str) -> str:
        self.events.append({"type": "task.changed", "task_id": row.task_id, "actor": actor})
        return "evt"

    def kick(self, team_id: str, *, reason: str, need_decision: bool = True) -> None:
        self.kicks.append((team_id, reason))

    def expire_approval(self, wait: Any) -> None:
        self.expired.append(str(wait.external_ref))


@pytest.fixture
def db(tmp_path: Path) -> SqlitePool:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return pool


@pytest.fixture
def repos(db: SqlitePool) -> SimpleNamespace:
    return SimpleNamespace(
        team_wait_repo=TeamWaitRepo(db),
        team_runtime_repo=TeamRuntimeRepo(db),
        team_task_repo=TeamTaskRepo(db),
        team_decision_repo=TeamDecisionRepo(db),
        team_event_repo=TeamEventRepo(db),
        agent_repo=AgentRepo(db),
        autonomy_policy_repo=AutonomyPolicyRepo(db),
    )


@pytest.fixture
def team_id(db: SqlitePool, repos: SimpleNamespace) -> str:
    user = UserRepo(db).create(username="alice", password_hash="h", role="user")
    repos.agent_repo.create(agent_id="team-01", user_id=user, name="crew", kind="team")
    repos.agent_repo.create(agent_id="member-1", user_id=user, name="m1", kind="expert")
    return "team-01"


def _runtime(repos: SimpleNamespace, team_id: str, *, status: str = "running") -> Any:
    row = repos.team_runtime_repo.ensure(team_id)
    return repos.team_runtime_repo.update(
        team_id,
        expected_version=row.version,
        fields={"status": status, "mode": "autopilot"},
    )


def _sweeper(
    repos: SimpleNamespace,
    coordinator: Coordinator,
    *,
    now: int | None = None,
) -> TeamSweeper:
    clock_now = now if now is not None else int(time.time())
    return TeamSweeper(
        repos=repos,
        coordinator=coordinator,
        timezone="UTC",
        clock=lambda: clock_now,
        decision_timeout_sec=600,
        aggregation_sec=5,
    )


@pytest.mark.asyncio
async def test_due_timer_settles_and_clears_task_wait(repos: SimpleNamespace, team_id: str) -> None:
    now = int(time.time())
    _runtime(repos, team_id)
    task = repos.team_task_repo.create(team_id=team_id, title="T")
    wait = repos.team_wait_repo.create(
        team_id,
        kind="timer",
        idempotency_key="timer:1",
        task_id=task.task_id,
        due_at=now - 1,
    )
    assert wait is not None
    repos.team_task_repo.update(
        task.task_id, expected_version=task.version, fields={"active_wait_id": wait.wait_id}
    )
    coordinator = Coordinator()
    sweeper = _sweeper(repos, coordinator, now=now)

    counts = await sweeper.run_once()

    assert counts["due"] == 1
    assert repos.team_wait_repo.get(wait.wait_id).status == "settled"  # type: ignore[union-attr]
    updated = repos.team_task_repo.get(task.task_id)
    assert updated is not None and updated.active_wait_id is None
    assert any(e.get("type") == "wait.due" for e in coordinator.events)


@pytest.mark.asyncio
async def test_paused_team_defers_due_timer(repos: SimpleNamespace, team_id: str) -> None:
    now = int(time.time())
    _runtime(repos, team_id, status="paused_user")
    wait = repos.team_wait_repo.create(
        team_id, kind="timer", idempotency_key="timer:1", due_at=now - 1
    )
    assert wait is not None
    coordinator = Coordinator()
    await _sweeper(repos, coordinator, now=now).run_once()

    assert repos.team_wait_repo.get(wait.wait_id).status == "deferred"  # type: ignore[union-attr]
    assert repos.team_wait_repo.reactivate_deferred(team_id) == 1
    assert repos.team_wait_repo.get(wait.wait_id).status == "waiting"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_timed_out_member_wait_parks_task(repos: SimpleNamespace, team_id: str) -> None:
    now = int(time.time())
    _runtime(repos, team_id)
    task = repos.team_task_repo.create(
        team_id=team_id, title="T", assignee="member-1", status="running"
    )
    wait = repos.team_wait_repo.create(
        team_id,
        kind="member_reply",
        idempotency_key="dispatch:1",
        task_id=task.task_id,
        timeout_at=now - 1,
    )
    assert wait is not None
    repos.team_task_repo.update(
        task.task_id, expected_version=task.version, fields={"active_wait_id": wait.wait_id}
    )
    coordinator = Coordinator()
    await _sweeper(repos, coordinator, now=now).run_once()

    assert repos.team_wait_repo.get(wait.wait_id).status == "timed_out"  # type: ignore[union-attr]
    updated = repos.team_task_repo.get(task.task_id)
    assert updated is not None and updated.status == "blocked"
    assert updated.active_wait_id is None
    assert any(e.get("type") == "wait.timeout" for e in coordinator.events)


@pytest.mark.asyncio
async def test_timed_out_approval_expires_hitl_pause(repos: SimpleNamespace, team_id: str) -> None:
    now = int(time.time())
    _runtime(repos, team_id)
    task = repos.team_task_repo.create(
        team_id=team_id, title="T", assignee="member-1", status="needs_input"
    )
    wait = repos.team_wait_repo.create(
        team_id,
        kind="approval",
        idempotency_key="approval:p1",
        task_id=task.task_id,
        external_ref="p1",
        timeout_at=now - 1,
    )
    assert wait is not None
    coordinator = Coordinator()
    await _sweeper(repos, coordinator, now=now).run_once()

    assert coordinator.expired == ["p1"]
    assert repos.team_wait_repo.get(wait.wait_id).status == "timed_out"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_stale_decision_fails_and_emits_event(repos: SimpleNamespace, team_id: str) -> None:
    now = int(time.time()) + 1000
    _runtime(repos, team_id, status="deciding")
    decision = repos.team_decision_repo.create(team_id, reason="goal.changed")
    repos.team_decision_repo.mark_running(decision.decision_id)
    coordinator = Coordinator()
    await _sweeper(repos, coordinator, now=now).run_once()

    row = repos.team_decision_repo.get(decision.decision_id)
    assert row is not None and row.status == "failed"
    assert any(e.get("type") == "decision.failed" for e in coordinator.events)
    runtime = repos.team_runtime_repo.get(team_id)
    assert runtime is not None and runtime.status == "running"


@pytest.mark.asyncio
async def test_heartbeat_records_and_kicks_open_work(repos: SimpleNamespace, team_id: str) -> None:
    now = int(time.time())
    row = _runtime(repos, team_id)
    repos.team_runtime_repo.update(
        team_id, expected_version=row.version, fields={"next_heartbeat_at": now - 1}
    )
    repos.team_task_repo.create(team_id=team_id, title="T")
    coordinator = Coordinator()
    await _sweeper(repos, coordinator, now=now).run_once()

    assert any(e.get("type") == "sweeper.heartbeat" for e in coordinator.events)
    assert coordinator.kicks == [(team_id, "sweeper.heartbeat")]
    updated = repos.team_runtime_repo.get(team_id)
    assert updated is not None and updated.next_heartbeat_at > now


@pytest.mark.asyncio
async def test_heartbeat_does_not_kick_while_waiting(repos: SimpleNamespace, team_id: str) -> None:
    now = int(time.time())
    row = _runtime(repos, team_id, status="waiting")
    repos.team_runtime_repo.update(
        team_id, expected_version=row.version, fields={"next_heartbeat_at": now - 1}
    )
    repos.team_task_repo.create(team_id=team_id, title="T")
    repos.team_wait_repo.create(team_id, kind="timer", idempotency_key="timer:1", due_at=now + 60)
    coordinator = Coordinator()
    await _sweeper(repos, coordinator, now=now).run_once()

    assert coordinator.kicks == []


@pytest.mark.asyncio
async def test_replay_routes_unconsumed_events(repos: SimpleNamespace, team_id: str) -> None:
    now = int(time.time()) + 100
    _runtime(repos, team_id)
    event = repos.team_event_repo.append(
        team_id, type="goal.changed", dedupe_key="goal:g:v1", actor="user:1"
    )
    assert event is not None
    coordinator = Coordinator()
    await _sweeper(repos, coordinator, now=now).run_once()

    assert coordinator.kicks == [(team_id, "goal.changed")]


@pytest.mark.asyncio
async def test_budget_day_rollover(repos: SimpleNamespace, team_id: str) -> None:
    now = int(time.time())
    row = _runtime(repos, team_id)
    repos.team_runtime_repo.update(
        team_id,
        expected_version=row.version,
        fields={"budget_day": "2000-01-01", "budget_used": 99},
    )
    await _sweeper(repos, Coordinator(), now=now).run_once()

    updated = repos.team_runtime_repo.get(team_id)
    assert updated is not None and updated.budget_day != "2000-01-01"
    assert updated.budget_used == 0


def test_recover_inflight_parks_tasks_lost_on_restart(repos: SimpleNamespace, team_id: str) -> None:
    now = int(time.time())
    _runtime(repos, team_id)
    task = repos.team_task_repo.create(
        team_id=team_id, title="T", assignee="member-1", status="running"
    )
    wait = repos.team_wait_repo.create(
        team_id,
        kind="member_reply",
        idempotency_key="dispatch:1",
        task_id=task.task_id,
        external_ref="job-lost",
    )
    assert wait is not None
    coordinator = Coordinator()

    assert _sweeper(repos, coordinator, now=now).recover_inflight() == 1

    assert repos.team_wait_repo.get(wait.wait_id).status == "timed_out"  # type: ignore[union-attr]
    updated = repos.team_task_repo.get(task.task_id)
    assert updated is not None and updated.status == "blocked"
    assert any(
        e.get("type") == "wait.timeout" and e.get("payload", {}).get("reason") == "process_restart"
        for e in coordinator.events
    )


def _windowed_sweeper(
    repos: SimpleNamespace, coordinator: Coordinator | None = None
) -> TeamSweeper:
    return TeamSweeper(repos=repos, coordinator=coordinator or Coordinator(), timezone="UTC")


def test_active_hours_block_wakes_outside_the_window(repos: SimpleNamespace, team_id: str) -> None:
    """A duty window must actually stop the agent from being woken."""
    from octop.infra.db.repos.autonomy_policy import AutonomyPolicy

    repos.autonomy_policy_repo.upsert(
        AutonomyPolicy(agent_id=team_id, active_hours_start="09:00", active_hours_end="17:00")
    )
    sweeper = _windowed_sweeper(repos)

    # 2026-10-29 03:00 UTC
    three_am = 1793242800
    assert sweeper._in_active_hours(team_id, three_am) is False
    # 2026-10-29 12:00 UTC
    noon = 1793275200
    assert sweeper._in_active_hours(team_id, noon) is True


def test_agent_without_policy_is_always_eligible(repos: SimpleNamespace, team_id: str) -> None:
    sweeper = _windowed_sweeper(repos)

    assert sweeper._in_active_hours(team_id, 1793242800) is True


@pytest.mark.asyncio
async def test_heartbeat_skips_the_kick_outside_active_hours(
    repos: SimpleNamespace, team_id: str
) -> None:
    """The duty window must gate the actual wake, not just the helper."""
    from octop.infra.db.repos.autonomy_policy import AutonomyPolicy

    # 2026-10-29 03:00 UTC, outside 09:00-17:00.
    now = 1793242800
    repos.autonomy_policy_repo.upsert(
        AutonomyPolicy(agent_id=team_id, active_hours_start="09:00", active_hours_end="17:00")
    )
    row = _runtime(repos, team_id)
    repos.team_runtime_repo.update(
        team_id, expected_version=row.version, fields={"next_heartbeat_at": now - 1}
    )
    repos.team_task_repo.create(team_id=team_id, title="T")
    coordinator = Coordinator()

    await _sweeper(repos, coordinator, now=now).run_once()

    assert coordinator.kicks == []
