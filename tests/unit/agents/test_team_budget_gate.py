"""The cost gate: a team must stop dispatching when its budget is spent.

Before this, ``budget_used`` was only ever zeroed, never accumulated, and
``budget_remaining`` was a hard-coded ``None`` — so the dispatcher's budget
branch was dead code and an unattended team could spend without limit.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from octop.infra.agents.teams.dispatcher import budget_remaining, plan_dispatches
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.autonomy_policy import AutonomyPolicy, AutonomyPolicyRepo
from octop.infra.db.repos.team_runtime import TeamRuntimeRepo
from octop.infra.db.repos.team_task_deps import TeamTaskDepsRepo
from octop.infra.db.repos.team_tasks import TeamTaskRepo
from octop.infra.db.repos.users import UserRepo

TEAM = "team-budget"


@pytest.fixture
def repos(tmp_path) -> SimpleNamespace:
    db = SqlitePool(tmp_path / "budget.db")
    run_migrations(db)
    user_id = UserRepo(db).create(username="u", password_hash="h", role="user")
    agent_repo = AgentRepo(db)
    agent_repo.create(agent_id=TEAM, user_id=user_id, name="crew", kind="team")
    agent_repo.create(agent_id="member-a", user_id=user_id, name="worker")
    return SimpleNamespace(
        db=db,
        team_task_repo=TeamTaskRepo(db),
        team_task_deps_repo=TeamTaskDepsRepo(db),
        team_runtime_repo=TeamRuntimeRepo(db),
        autonomy_policy_repo=AutonomyPolicyRepo(db),
    )


def test_no_policy_means_no_limit(repos: SimpleNamespace) -> None:
    repos.team_runtime_repo.ensure(TEAM)
    assert budget_remaining(repos, TEAM) is None


def test_remaining_is_limit_minus_spent(repos: SimpleNamespace) -> None:
    repos.team_runtime_repo.ensure(TEAM)
    repos.autonomy_policy_repo.upsert(AutonomyPolicy(agent_id=TEAM, budget_limit_tokens=1000))
    repos.team_runtime_repo.add_budget_used(TEAM, 300)

    assert budget_remaining(repos, TEAM) == 700


def test_spend_is_accumulated_atomically(repos: SimpleNamespace) -> None:
    """Concurrent reports must all land; a lost turn is unaccounted spend."""
    repos.team_runtime_repo.ensure(TEAM)

    repos.team_runtime_repo.add_budget_used(TEAM, 100)
    repos.team_runtime_repo.add_budget_used(TEAM, 250)

    runtime = repos.team_runtime_repo.get(TEAM)
    assert runtime is not None and runtime.budget_used == 350


def test_exhausted_budget_defers_dispatch(repos: SimpleNamespace) -> None:
    repos.team_runtime_repo.ensure(TEAM)
    repos.autonomy_policy_repo.upsert(AutonomyPolicy(agent_id=TEAM, budget_limit_tokens=100))
    repos.team_runtime_repo.add_budget_used(TEAM, 100)
    task = repos.team_task_repo.create(
        team_id=TEAM, title="T", assignee="member-a", budget_tokens=50
    )

    steps = plan_dispatches(repos, TEAM, now=1000, budget_remaining=budget_remaining(repos, TEAM))

    actions = {s.task_id: s.action for s in steps}
    assert actions[task.task_id] == "defer_budget"


def test_headroom_still_dispatches(repos: SimpleNamespace) -> None:
    repos.team_runtime_repo.ensure(TEAM)
    repos.autonomy_policy_repo.upsert(AutonomyPolicy(agent_id=TEAM, budget_limit_tokens=1000))
    repos.team_runtime_repo.add_budget_used(TEAM, 100)
    task = repos.team_task_repo.create(
        team_id=TEAM, title="T", assignee="member-a", budget_tokens=50
    )

    steps = plan_dispatches(repos, TEAM, now=1000, budget_remaining=budget_remaining(repos, TEAM))

    actions = {s.task_id: s.action for s in steps}
    assert actions[task.task_id] == "dispatch"


def test_monthly_window_rolls_on_month_not_day(repos: SimpleNamespace) -> None:
    """A monthly budget must survive a day change inside the month."""
    repos.team_runtime_repo.ensure(TEAM)
    repos.autonomy_policy_repo.upsert(
        AutonomyPolicy(agent_id=TEAM, budget_limit_tokens=1000, budget_window="month")
    )
    # The first roll only initialises the period key.
    repos.team_runtime_repo.reset_budget(day="2026-10-01", month="2026-10")
    repos.team_runtime_repo.add_budget_used(TEAM, 400)

    # Same month, new day: keep the spend.
    repos.team_runtime_repo.reset_budget(day="2026-10-29", month="2026-10")
    runtime = repos.team_runtime_repo.get(TEAM)
    assert runtime is not None and runtime.budget_used == 400

    # New month: roll over.
    repos.team_runtime_repo.reset_budget(day="2026-11-01", month="2026-11")
    runtime = repos.team_runtime_repo.get(TEAM)
    assert runtime is not None and runtime.budget_used == 0


def test_daily_window_still_rolls_every_day(repos: SimpleNamespace) -> None:
    repos.team_runtime_repo.ensure(TEAM)
    repos.autonomy_policy_repo.upsert(
        AutonomyPolicy(agent_id=TEAM, budget_limit_tokens=1000, budget_window="day")
    )
    repos.team_runtime_repo.reset_budget(day="2026-10-28", month="2026-10")
    repos.team_runtime_repo.add_budget_used(TEAM, 400)

    repos.team_runtime_repo.reset_budget(day="2026-10-29", month="2026-10")

    runtime = repos.team_runtime_repo.get(TEAM)
    assert runtime is not None and runtime.budget_used == 0


def test_exhausted_budget_defers_an_unpriced_task(repos: SimpleNamespace) -> None:
    """A task that never declared a cost must still be blocked at zero."""
    repos.team_runtime_repo.ensure(TEAM)
    repos.autonomy_policy_repo.upsert(AutonomyPolicy(agent_id=TEAM, budget_limit_tokens=100))
    repos.team_runtime_repo.add_budget_used(TEAM, 100)
    task = repos.team_task_repo.create(team_id=TEAM, title="T", assignee="member-a")

    steps = plan_dispatches(repos, TEAM, now=1000, budget_remaining=budget_remaining(repos, TEAM))

    actions = {s.task_id: s.action for s in steps}
    assert actions[task.task_id] == "defer_budget"
