"""Unit tests for the shadow team coordinator (docs/team-drive-v2.md §5, phase 1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from octop.infra.agents.teams.coordinator import TeamCoordinator, _classify
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.team_events import TeamEventRepo
from octop.infra.db.repos.team_goals import TeamGoalRepo
from octop.infra.db.repos.team_runtime import TeamRuntimeRepo
from octop.infra.db.repos.team_tasks import TeamTaskRepo
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


def _coordinator(db: SqlitePool) -> TeamCoordinator:
    return TeamCoordinator(
        event_repo=TeamEventRepo(db),
        runtime_repo=TeamRuntimeRepo(db),
        task_repo=TeamTaskRepo(db),
    )


def test_record_ensures_runtime_and_dedupes(db: SqlitePool, team_id: str) -> None:
    coord = _coordinator(db)
    event_id = coord.record(team_id, type="member.replied", dedupe_key="inbox:1", actor="member:m1")
    assert event_id is not None
    runtime = TeamRuntimeRepo(db).get(team_id)
    assert runtime is not None and runtime.status == "idle"
    events = TeamEventRepo(db).list_after(team_id)
    assert [e.type for e in events] == ["member.replied"]
    # Same dedupe key is not appended twice.
    assert coord.record(team_id, type="member.replied", dedupe_key="inbox:1") is None
    assert len(TeamEventRepo(db).list_after(team_id)) == 1


def test_record_skips_without_team_scope(db: SqlitePool) -> None:
    coord = _coordinator(db)
    assert coord.record(None, type="task.changed", dedupe_key="task:1:v1") is None
    assert coord.record("", type="task.changed", dedupe_key="task:1:v1") is None
    assert TeamEventRepo(db).list_after("team-01") == []


def test_classify_rules() -> None:
    assert _classify("goal.changed", None) == "judgment"
    assert _classify("member.replied", {"status": "failed"}) == "judgment"
    assert _classify("member.replied", {"status": "done"}) == "routine"
    assert _classify("approval.resolved", {"status": "rejected"}) == "judgment"
    assert _classify("approval.resolved", {"status": "approved", "task_id": "t1"}) == "routine"
    # A team-level approval has no task to resume, so it needs a judgment.
    assert _classify("approval.resolved", {"status": "approved"}) == "judgment"
    assert _classify("task.changed", {"status": "done"}) == "routine"
    assert _classify("user.message", {"excerpt": "hi"}) == "routine"


def test_classification_counters(db: SqlitePool, team_id: str) -> None:
    from octop.infra.metrics import METRICS  # noqa: PLC0415

    before = METRICS.snapshot()
    coord = _coordinator(db)
    coord.record(team_id, type="task.changed", dedupe_key="task:1:v1")
    coord.record(team_id, type="member.replied", dedupe_key="inbox:1", payload={"status": "done"})
    coord.record(team_id, type="member.replied", dedupe_key="inbox:2", payload={"status": "failed"})
    after = METRICS.snapshot()
    assert after["team_event_total"] - before["team_event_total"] == 3
    assert after["team_event_routine_total"] - before["team_event_routine_total"] == 2
    assert after["team_event_judgment_total"] - before["team_event_judgment_total"] == 1


def test_record_member_reply_helper(db: SqlitePool, team_id: str) -> None:
    coord = _coordinator(db)
    event_id = coord.record_member_reply(
        team_id=team_id,
        member_id="member-1",
        status="done",
        inbox_id="job-9",
        excerpt="done deal",
    )
    assert event_id is not None
    row = TeamEventRepo(db).list_after(team_id)[0]
    assert row.dedupe_key == "inbox:job-9"
    assert row.actor == "member:member-1"
    assert row.payload == {"status": "done", "member_id": "member-1", "excerpt": "done deal"}


def test_task_and_goal_change_helpers(db: SqlitePool, team_id: str) -> None:
    goal = TeamGoalRepo(db).create(
        team_id=team_id, title="G1", created_by=1, description="", success_criteria=""
    )
    task = TeamTaskRepo(db).create(team_id=team_id, title="T1", goal_id=goal.goal_id)
    coord = _coordinator(db)
    assert coord.record_goal_change(goal, actor="user:1") is not None
    assert coord.record_task_change(task, actor="host") is not None
    events = TeamEventRepo(db).list_after(team_id)
    assert [(e.type, e.actor) for e in events] == [
        ("goal.changed", "user:1"),
        ("task.changed", "host"),
    ]
    assert events[0].dedupe_key.startswith(f"goal:{goal.goal_id}:{goal.updated_at}:")
    assert events[1].dedupe_key == f"task:{task.task_id}:v{task.version}"
    assert events[1].payload == {
        "task_id": task.task_id,
        "status": task.status,
        "title": "T1",
    }


def test_record_hitl_resolved_resolves_team_through_task(db: SqlitePool, team_id: str) -> None:
    task = TeamTaskRepo(db).create(team_id=team_id, title="T1")
    coord = _coordinator(db)
    event_id = coord.record_hitl_resolved(
        task_id=task.task_id, pending_id="ab12cd34", status="rejected", agent_id="member-1"
    )
    assert event_id is not None
    row = TeamEventRepo(db).list_after(team_id)[0]
    assert row.type == "approval.resolved"
    assert row.payload["status"] == "rejected"
    assert row.payload["task_id"] == task.task_id
    # Non-team pauses and unknown tasks are not ledgered.
    assert coord.record_hitl_resolved(task_id=None, pending_id="x", status="approved") is None
    assert coord.record_hitl_resolved(task_id="missing", pending_id="x", status="approved") is None


def test_storage_failure_is_swallowed(db: SqlitePool, team_id: str) -> None:
    class BoomRepo:
        def append(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("disk on fire")

    coord = TeamCoordinator(event_repo=BoomRepo(), runtime_repo=None)
    assert coord.record(team_id, type="task.changed", dedupe_key="task:1:v1") is None


def test_two_goal_edits_in_one_second_both_land(db: SqlitePool, team_id: str) -> None:
    """Second-resolution ``updated_at`` must not swallow the second edit.

    The event ledger dedupes on a UNIQUE key. Keying it on ``updated_at``
    meant two edits inside one second produced the same key, so the second
    event vanished: the board kept the stale goal and the coordinator never
    re-planned.
    """
    repo = TeamGoalRepo(db)
    goal = repo.create(team_id=team_id, title="G1", created_by=1)
    coord = _coordinator(db)

    first = coord.record_goal_change(goal, actor="user:1")
    # Same stored row (same updated_at) — models two edits landing within
    # one second, which is exactly what a fast double-PATCH produces.
    second = coord.record_goal_change(goal, actor="user:1")

    assert first is not None
    assert second is not None
    assert first != second
    assert len([e for e in TeamEventRepo(db).list_after(team_id) if e.type == "goal.changed"]) == 2
