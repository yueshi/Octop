"""Digital-employee autonomy columns (docs/digital-employee-plan.md S1.1).

The columns ship inside the unreleased v25 pair: SQLite has no
``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``, so the bare ALTERs in
``025_light_apps.sql`` are applied by ``migrate._ensure_autonomy_schema``
instead. These tests pin the three cases that helper exists for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from octop.infra.db.migrate import _ensure_autonomy_schema, run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo

AGENT_AUTONOMY_COLUMNS = {"autonomy_enabled"}
POLICY_COLUMNS = {
    "budget_limit_tokens",
    "budget_window",
    "sla_tasks_per_day",
    "escalate_to",
    "responsible_scope",
    "tool_scope_allow",
    "tool_scope_deny",
}


@pytest.fixture
def db(tmp_path: Path) -> SqlitePool:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return pool


def _columns(pool: SqlitePool, table: str) -> set[str]:
    with pool.connect() as conn:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_autonomy_columns_present_after_migrations(db: SqlitePool) -> None:
    assert AGENT_AUTONOMY_COLUMNS.issubset(_columns(db, "agents"))
    assert POLICY_COLUMNS.issubset(_columns(db, "proactive_care_config"))


def test_autonomy_columns_have_expected_defaults(db: SqlitePool) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO agents(agent_id, user_id, name, enabled, created_at, updated_at) "
            "VALUES ('a1', NULL, 'solo', 1, 0, 0)"
        )
        conn.execute(
            "INSERT INTO proactive_care_config(agent_id, enabled, updated_at) VALUES ('a1', 1, 0)"
        )
        agent = conn.execute("SELECT autonomy_enabled FROM agents WHERE agent_id = 'a1'").fetchone()
        policy = conn.execute(
            "SELECT budget_limit_tokens, budget_window, sla_tasks_per_day, escalate_to, "
            "responsible_scope, tool_scope_allow, tool_scope_deny "
            "FROM proactive_care_config WHERE agent_id = 'a1'"
        ).fetchone()
    # Autonomy is opt-in: a fresh agent must not become a digital employee.
    assert int(agent["autonomy_enabled"]) == 0
    assert int(policy["budget_limit_tokens"]) == 0
    assert str(policy["budget_window"]) == "day"
    assert int(policy["sla_tasks_per_day"]) == 0
    assert str(policy["escalate_to"]) == ""
    assert str(policy["responsible_scope"]) == ""
    assert str(policy["tool_scope_allow"]) == ""
    assert str(policy["tool_scope_deny"]) == ""


def test_ensure_autonomy_schema_adds_missing_columns(db: SqlitePool) -> None:
    """The helper itself is the SQLite repair path — pin it directly."""
    with db.connect() as conn:
        conn.execute("ALTER TABLE agents DROP COLUMN autonomy_enabled")
        conn.execute("ALTER TABLE proactive_care_config DROP COLUMN escalate_to")
    assert "autonomy_enabled" not in _columns(db, "agents")

    _ensure_autonomy_schema(db)

    assert AGENT_AUTONOMY_COLUMNS.issubset(_columns(db, "agents"))
    assert POLICY_COLUMNS.issubset(_columns(db, "proactive_care_config"))


def test_ensure_autonomy_schema_is_idempotent(db: SqlitePool) -> None:
    _ensure_autonomy_schema(db)
    _ensure_autonomy_schema(db)

    assert AGENT_AUTONOMY_COLUMNS.issubset(_columns(db, "agents"))
    assert POLICY_COLUMNS.issubset(_columns(db, "proactive_care_config"))


def test_columns_repaired_when_version_already_recorded(tmp_path: Path) -> None:
    """A database that already recorded v25 must still gain the columns.

    ``run_migrations`` skips files whose version is already recorded, so the
    repair has to come from the unconditional helper block.
    """
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    # Simulate a build that recorded the version before the columns existed.
    with pool.connect() as conn:
        conn.execute("ALTER TABLE agents DROP COLUMN autonomy_enabled")
        conn.execute("ALTER TABLE proactive_care_config DROP COLUMN budget_window")
    assert "autonomy_enabled" not in _columns(pool, "agents")

    run_migrations(pool)

    assert AGENT_AUTONOMY_COLUMNS.issubset(_columns(pool, "agents"))
    assert POLICY_COLUMNS.issubset(_columns(pool, "proactive_care_config"))


def test_columns_survive_version_rewind(tmp_path: Path) -> None:
    """Re-running v25 must not fail on the bare ALTERs (SQLite has no IF NOT EXISTS)."""
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    with pool.connect() as conn:
        conn.execute("UPDATE _schema_version SET version = 21")

    run_migrations(pool)

    assert AGENT_AUTONOMY_COLUMNS.issubset(_columns(pool, "agents"))
    assert POLICY_COLUMNS.issubset(_columns(pool, "proactive_care_config"))
    with pool.connect() as conn:
        version = conn.execute("SELECT version FROM _schema_version").fetchone()[0]
    assert version == 29


def test_agent_repo_reads_autonomy_enabled(db: SqlitePool) -> None:
    repo = AgentRepo(db)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO agents(agent_id, user_id, name, enabled, created_at, updated_at, "
            "autonomy_enabled) VALUES ('solo', NULL, 'solo', 1, 0, 0, 1)"
        )
    row = repo.get("solo")
    assert row is not None
    assert row.autonomy_enabled == 1


def test_agent_repo_defaults_to_inactive(db: SqlitePool) -> None:
    repo = AgentRepo(db)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO agents(agent_id, user_id, name, enabled, created_at, updated_at) "
            "VALUES ('plain', NULL, 'plain', 1, 0, 0)"
        )
    row = repo.get("plain")
    assert row is not None
    assert row.autonomy_enabled == 0


def test_agent_repo_tolerates_missing_column(db: SqlitePool) -> None:
    """A database that never received the column must still load agents."""
    repo = AgentRepo(db)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO agents(agent_id, user_id, name, enabled, created_at, updated_at) "
            "VALUES ('old', NULL, 'old', 1, 0, 0)"
        )
        conn.execute("ALTER TABLE agents DROP COLUMN autonomy_enabled")

    row = repo.get("old")
    assert row is not None
    assert row.autonomy_enabled == 0
