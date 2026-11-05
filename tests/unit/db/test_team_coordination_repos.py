"""Unit tests for the team coordination repos (docs/team-drive-v2.md §4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from octop.infra.db.migrate import _ensure_team_board_schema, run_migrations
from octop.infra.db.pool import DatabasePool, SqlitePool
from octop.infra.db.repos.team_decisions import TeamDecisionRepo
from octop.infra.db.repos.team_events import TeamEventRepo
from octop.infra.db.repos.team_runtime import RUNTIME_OPEN_STATUSES, TeamRuntimeRepo
from octop.infra.db.repos.team_task_deps import TeamTaskDepsRepo
from octop.infra.db.repos.team_tasks import TeamTaskRepo
from octop.infra.db.repos.team_waits import TeamWaitRepo
from octop.infra.db.repos.usage import UsageRepo

TEAM_TABLES = (
    "team_runtime",
    "team_events",
    "team_decisions",
    "team_waits",
    "team_task_deps",
)


@pytest.fixture
def pool(tmp_path: Path) -> SqlitePool:
    p = SqlitePool(tmp_path / "octop.db")
    run_migrations(p)
    return p


def seed_team(pool: DatabasePool, team_id: str) -> None:
    """Insert a minimal team host row — coordination tables FK onto agents."""
    with pool.transaction() as conn:
        conn.execute(
            "INSERT INTO agents(agent_id, name, kind, created_at, updated_at) "
            "VALUES (?, ?, 'team', 0, 0)",
            (team_id, team_id),
        )


def seed_user(pool: DatabasePool, username: str) -> int:
    with pool.transaction() as conn:
        conn.execute(
            "INSERT INTO users(username, password_hash, role, created_at) "
            "VALUES (?, 'x', 'admin', 0)",
            (username,),
        )
        row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    return int(row["id"])


def test_coordination_schema_present(pool: SqlitePool) -> None:
    with pool.connect() as conn:
        tables = {
            str(r["name"])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        task_cols = {
            str(r["name"]) for r in conn.execute("PRAGMA table_info(team_tasks)").fetchall()
        }
        usage_cols = {
            str(r["name"]) for r in conn.execute("PRAGMA table_info(usage_log)").fetchall()
        }
    for name in TEAM_TABLES:
        assert name in tables
    for col in (
        "window_start",
        "window_end",
        "budget_tokens",
        "concurrency_key",
        "escalation_policy_json",
        "idempotency_key",
        "active_wait_id",
    ):
        assert col in task_cols
    assert "team_id" in usage_cols


def test_runtime_ensure_and_cas(pool: SqlitePool) -> None:
    seed_team(pool, "team-x")
    repo = TeamRuntimeRepo(pool)
    row = repo.ensure("team-x")
    assert row.status == "idle"
    assert row.version == 1
    assert repo.ensure("team-x").version == 1  # ensure never bumps version

    updated = repo.update(
        "team-x",
        expected_version=1,
        fields={"status": "running", "mode": "autopilot"},
    )
    assert updated is not None
    assert updated.status == "running"
    assert updated.version == 2
    assert repo.update("team-x", expected_version=1, fields={"status": "idle"}) is None
    assert [r.team_id for r in repo.list_open()] == ["team-x"]
    assert all(r.status in RUNTIME_OPEN_STATUSES for r in repo.list_open())


def test_events_append_dedupe_and_seq(pool: SqlitePool) -> None:
    seed_team(pool, "team-x")
    seed_team(pool, "team-y")
    repo = TeamEventRepo(pool)
    first = repo.append("team-x", type="member.replied", dedupe_key="inbox:1", actor="member:m1")
    second = repo.append("team-x", type="task.changed", dedupe_key="task:1:v1")
    assert first is not None and first.seq == 1
    assert second is not None and second.seq == 2
    # Same dedupe key is swallowed, another team's seq starts at 1.
    assert repo.append("team-x", type="task.changed", dedupe_key="inbox:1") is None
    other = repo.append("team-y", type="user.message", dedupe_key="msg:9")
    assert other is not None and other.seq == 1

    assert [e.seq for e in repo.list_after("team-x")] == [1, 2]
    assert second is not None
    repo.mark_consumed(second.event_id, decision_id="d1")
    got = repo.get(second.event_id)
    assert got is not None
    assert got.consumed_at is not None
    assert got.decision_id == "d1"


def test_decision_lifecycle(pool: SqlitePool) -> None:
    seed_team(pool, "team-x")
    repo = TeamDecisionRepo(pool)
    decision = repo.create("team-x", trigger_event_ids=["e1"], reason="member_failed")
    assert decision.status == "pending"
    assert repo.get_active("team-x") is not None

    repo.mark_running(decision.decision_id, thread_id="thr-1", budget_reserved=100)
    row = repo.get(decision.decision_id)
    assert row is not None
    assert row.status == "running"
    assert row.started_at is not None
    assert row.budget_reserved == 100

    repo.finish(decision.decision_id, status="committed", plan={"noop": True}, cost_tokens=42)
    row = repo.get(decision.decision_id)
    assert row is not None
    assert row.plan == {"noop": True}
    assert row.cost_tokens == 42
    assert row.finished_at is not None
    assert repo.get_active("team-x") is None


def test_waits_settle_idempotent_and_scans(pool: SqlitePool) -> None:
    seed_team(pool, "team-x")
    repo = TeamWaitRepo(pool)
    wait = repo.create(
        "team-x",
        kind="member_reply",
        idempotency_key="dispatch:t1:1",
        timeout_at=100,
        payload={"message": "hi"},
    )
    assert wait is not None
    assert wait.status == "waiting"
    assert repo.create("team-x", kind="member_reply", idempotency_key="dispatch:t1:1") is None
    assert repo.get_by_idempotency_key("dispatch:t1:1") is not None

    assert repo.settle(wait.wait_id, status="settled", external_ref="job-9") is True
    assert repo.settle(wait.wait_id, status="cancelled") is False
    got = repo.get(wait.wait_id)
    assert got is not None
    assert got.status == "settled"
    assert got.settled_at is not None
    assert got.external_ref == "job-9"

    due_soon = repo.create("team-x", kind="timer", idempotency_key="timer:t1:200", due_at=200)
    due_late = repo.create(
        "team-x", kind="timer", idempotency_key="timer:t1:900", due_at=900, timeout_at=950
    )
    assert due_soon is not None and due_late is not None
    assert [w.wait_id for w in repo.list_due(now=250)] == [due_soon.wait_id]
    assert [w.wait_id for w in repo.list_timed_out(now=960)] == [due_late.wait_id]
    assert repo.settle(due_soon.wait_id, status="settled") is True
    # due_late is still waiting with due_at=900 <= 999.
    assert [w.wait_id for w in repo.list_due(now=999)] == [due_late.wait_id]


def test_task_deps_edges(pool: SqlitePool) -> None:
    seed_team(pool, "team-x")
    tasks = TeamTaskRepo(pool)
    a = tasks.create(team_id="team-x", title="A")
    b = tasks.create(team_id="team-x", title="B")
    repo = TeamTaskDepsRepo(pool)

    assert repo.add(a.task_id, a.task_id) is False  # self-dependency
    assert repo.add(a.task_id, b.task_id) is True
    assert repo.add(a.task_id, b.task_id) is False  # duplicate edge is a no-op
    assert repo.list_for_task(a.task_id) == [b.task_id]
    assert [d.depends_on_task_id for d in repo.list_for_team("team-x")] == [b.task_id]

    repo.clear_for_task(a.task_id)
    assert repo.list_for_task(a.task_id) == []
    assert repo.list_for_team("team-x") == []


def test_usage_team_id_roundtrip(pool: SqlitePool) -> None:
    uid = seed_user(pool, "u1")
    seed_team(pool, "team-x")
    seed_team(pool, "member-1")  # usage_log.agent_id FKs onto agents
    repo = UsageRepo(pool)
    repo.record(
        agent_id="team-x",
        user_id=uid,
        input_tokens=10,
        output_tokens=5,
        source="cron",
        team_id="team-x",
    )
    repo.record(
        agent_id="member-1",
        user_id=uid,
        input_tokens=7,
        output_tokens=3,
        source="team_member",
    )
    with pool.connect() as conn:
        rows = conn.execute("SELECT agent_id, team_id FROM usage_log ORDER BY agent_id").fetchall()
    by_agent = {str(r["agent_id"]): r["team_id"] for r in rows}
    assert by_agent["team-x"] == "team-x"
    assert by_agent["member-1"] is None


def test_team_board_repairs_pre_coordination_shape(pool: SqlitePool) -> None:
    """A DB that recorded v25 with the old team_tasks shape still migrates."""
    with pool.transaction() as conn:
        conn.execute("DROP TABLE IF EXISTS team_task_deps")
        conn.execute("DROP TABLE IF EXISTS team_tasks")
        conn.execute(
            """
            CREATE TABLE team_tasks (
              id             INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id        TEXT NOT NULL UNIQUE,
              team_id        TEXT NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
              goal_id        TEXT REFERENCES team_goals(goal_id) ON DELETE SET NULL,
              title          TEXT NOT NULL,
              spec           TEXT NOT NULL DEFAULT '',
              assignee       TEXT REFERENCES agents(agent_id) ON DELETE SET NULL,
              status         TEXT NOT NULL DEFAULT 'proposed',
              priority       INTEGER NOT NULL DEFAULT 0,
              result_summary TEXT NOT NULL DEFAULT '',
              blocked_reason TEXT NOT NULL DEFAULT '',
              version        INTEGER NOT NULL DEFAULT 1,
              created_at     INTEGER NOT NULL,
              updated_at     INTEGER NOT NULL,
              due_at         INTEGER
            )
            """
        )
    _ensure_team_board_schema(pool)
    with pool.connect() as conn:
        cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(team_tasks)").fetchall()}
        indexes = {str(r["name"]) for r in conn.execute("PRAGMA index_list(team_tasks)").fetchall()}
    assert {
        "window_start",
        "window_end",
        "budget_tokens",
        "concurrency_key",
        "escalation_policy_json",
        "idempotency_key",
        "active_wait_id",
    } <= cols
    assert "idx_team_tasks_idem" in indexes


def test_usage_team_backfill_is_idempotent(pool: SqlitePool) -> None:
    uid = seed_user(pool, "u1")
    with pool.transaction() as conn:
        conn.execute(
            "INSERT INTO agents(agent_id, name, kind, user_id, created_at, updated_at) "
            "VALUES ('team-x', 'T', 'team', ?, 0, 0)",
            (uid,),
        )
        conn.execute(
            "INSERT INTO usage_log(ts, agent_id, user_id, input_tokens, output_tokens, "
            "total_tokens, source) VALUES (1, 'team-x', ?, 10, 5, 15, 'cron')",
            (uid,),
        )
    # DBs that recorded v25 before the team_id fold repair through this helper;
    # running it twice must be a no-op, not a reset.
    _ensure_team_board_schema(pool)
    _ensure_team_board_schema(pool)
    with pool.connect() as conn:
        r = conn.execute("SELECT team_id FROM usage_log WHERE agent_id = 'team-x'").fetchone()
    assert r is not None and r["team_id"] == "team-x"
