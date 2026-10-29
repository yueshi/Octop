"""INV-4: every usage path must credit the same budget owner.

A digital employee's spend has to reach ``team_runtime.budget_used`` — the
counter ``budget_remaining`` gates on. If the attribution path skips it, the
gate is decorative.
"""

from __future__ import annotations

from types import SimpleNamespace

from octop.infra.agents.teams.service import usage_owner_id
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.team_runtime import TeamRuntimeRepo
from octop.infra.db.repos.usage import UsageRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.gateway.process.usage_record import record_turn_usage


def _agent(**overrides):
    base = SimpleNamespace(agent_id="a1", kind="expert", autonomy_enabled=0)
    return SimpleNamespace(**{**vars(base), **overrides})


def test_team_host_and_digital_employee_are_metered() -> None:
    assert usage_owner_id(_agent(agent_id="crew", kind="team")) == "crew"
    assert usage_owner_id(_agent(agent_id="solo", autonomy_enabled=1)) == "solo"


def test_ordinary_agents_stay_unattributed() -> None:
    assert usage_owner_id(_agent()) is None
    assert usage_owner_id(None) is None


def test_recorded_usage_credits_the_budget_counter(tmp_path) -> None:
    db = SqlitePool(tmp_path / "usage.db")
    run_migrations(db)
    user_id = UserRepo(db).create(username="u", password_hash="h", role="user")
    AgentRepo(db).create(agent_id="solo", user_id=user_id, name="solo")
    runtime_repo = TeamRuntimeRepo(db)
    runtime_repo.ensure("solo")

    record_turn_usage(
        UsageRepo(db),
        agent_id="solo",
        user_id=user_id,
        thread_id="t1",
        usage={"input_tokens": 120, "output_tokens": 30},
        team_id="solo",
        runtime_repo=runtime_repo,
    )

    runtime = runtime_repo.get("solo")
    assert runtime is not None
    assert runtime.budget_used == 150


def test_usage_without_an_owner_does_not_touch_any_counter(tmp_path) -> None:
    db = SqlitePool(tmp_path / "usage2.db")
    run_migrations(db)
    user_id = UserRepo(db).create(username="u", password_hash="h", role="user")
    AgentRepo(db).create(agent_id="solo", user_id=user_id, name="solo")
    runtime_repo = TeamRuntimeRepo(db)
    runtime_repo.ensure("solo")

    record_turn_usage(
        UsageRepo(db),
        agent_id="solo",
        user_id=int(user_id),
        thread_id="t1",
        usage={"input_tokens": 10, "output_tokens": 5},
        team_id=None,
        runtime_repo=runtime_repo,
    )

    runtime = runtime_repo.get("solo")
    assert runtime is not None and runtime.budget_used == 0
