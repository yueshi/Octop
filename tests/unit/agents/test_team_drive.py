"""Unit tests for the team drive loop: tools, guardrails, stale reaping."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langgraph.config import var_child_runnable_config

from octop.config import OctopConfig
from octop.infra.agents.manager import AgentManager
from octop.infra.agents.teams.drive import (
    reap_stale_tasks,
)
from octop.infra.agents.teams.drive_tools import (
    TEAM_DRIVE_TOOL_NAMES,
    build_team_drive_tools,
)
from octop.infra.agents.tool_scope import tool_scope
from octop.infra.cron.delivery import CronDeliveryService
from octop.infra.cron.manager import CronManager
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.team_tasks import TeamTaskRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.db.services import build_shared_services
from octop.infra.utils.paths import PathLayout


@contextmanager
def _configurable(**kwargs: object):
    token = var_child_runnable_config.set({"configurable": kwargs})
    try:
        yield
    finally:
        var_child_runnable_config.reset(token)


def _gateway() -> MagicMock:
    gw = MagicMock()
    gw.thread_registry = MagicMock()
    gw.thread_registry.get_session = MagicMock(return_value=None)
    gw.thread_registry.get_or_create_by_key = AsyncMock(return_value="thr_mock")
    return gw


def _cron_manager(services: Any) -> CronManager:
    gw = _gateway()
    mgr = CronManager(
        gateway=gw,
        delivery_service=CronDeliveryService(
            gateway=gw,
            agent_manager=MagicMock(),
            repos=services.repos,
        ),
        repos=services.repos,
        timezone="UTC",
    )
    fake_scheduler = MagicMock()
    fake_scheduler.running = False
    fake_scheduler.get_job = MagicMock(return_value=None)
    fake_scheduler.get_jobs = MagicMock(return_value=[])
    mgr._scheduler = fake_scheduler
    return mgr


@pytest.fixture
def db(tmp_path: Path) -> SqlitePool:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return pool


@pytest.fixture
def services(tmp_path: Path) -> Any:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    paths = PathLayout(tmp_path)
    paths.ensure_root()
    return build_shared_services(db=pool, paths=paths, config=OctopConfig())


@pytest.fixture
def env(services: Any) -> dict[str, Any]:
    repos = services.repos
    alice = repos.user_repo.create(username="alice", password_hash="h", role="user")
    bob = repos.user_repo.create(username="bob", password_hash="h", role="user")
    repos.agent_repo.create(agent_id="team-01", user_id=alice, name="crew", kind="team")
    repos.agent_repo.create(agent_id="member-01", user_id=alice, name="worker")
    return {
        "alice": alice,
        "bob": bob,
        "team_id": "team-01",
        "member_id": "member-01",
        "task_repo": repos.team_task_repo,
        "agent_repo": repos.agent_repo,
        "cron_repo": repos.cron_repo,
        "cron_manager": _cron_manager(services),
        "user_repo": repos.user_repo,
    }


def _tools(env: dict[str, Any], **builder_kwargs: Any) -> dict[str, Any]:
    tools = build_team_drive_tools(
        cron_manager=env["cron_manager"],
        task_repo=env["task_repo"],
        agent_repo=env["agent_repo"],
        cron_repo=env["cron_repo"],
        user_repo=env["user_repo"],
        **builder_kwargs,
    )
    assert [t.name for t in tools] == list(TEAM_DRIVE_TOOL_NAMES)
    return {t.name: t for t in tools}


async def _call(env: dict[str, Any], user_id: int, tool: Any, **args: Any) -> dict[str, Any]:
    with tool_scope(env["team_id"], user_id), _configurable():
        raw = await tool.ainvoke(args)
    return json.loads(raw)


@pytest.mark.asyncio
async def test_drive_enable_rejects_bad_triggers(env: dict[str, Any]) -> None:
    tools = _tools(env)
    for bad in ("interval:60", "cron:0 9 * * *", "agently:inst-1", "date:nope"):
        out = await _call(env, env["alice"], tools["drive_enable"], trigger=bad)
        assert "error" in out, bad
    assert env["cron_manager"].list_by_agent(env["team_id"]) == []


@pytest.mark.asyncio
async def test_stop_team_drive_clears_legacy_rows_and_open_work(
    env: dict[str, Any],
) -> None:
    """The kill switch still cleans up cron rows left by the v1 loop."""
    from octop.infra.agents.teams.drive import TEAM_DRIVE_KIND, stop_team_drive
    from octop.infra.cron.manager import CronCreateSpec

    await env["cron_manager"].create(
        CronCreateSpec(
            cron_id="drive-team-01",
            agent_id=env["team_id"],
            user_id=env["alice"],
            name="drive",
            trigger="interval:3600",
            prompt="wake",
            fresh_thread=False,
            session_key="k",
            task_type="agent",
            enabled=True,
            meta={"kind": TEAM_DRIVE_KIND},
        )
    )
    task_repo = env["task_repo"]
    task_repo.create(team_id=env["team_id"], title="open-1", status="running")
    task_repo.create(team_id=env["team_id"], title="open-2")
    task_repo.create(team_id=env["team_id"], title="closed", status="done")

    inbox = MagicMock()
    inbox.list = MagicMock(return_value=[MagicMock(id="inbox-1"), MagicMock(id="inbox-2")])
    inbox.cancel = MagicMock(side_effect=[True, False])

    out = await stop_team_drive(
        team_id=env["team_id"],
        cron_manager=env["cron_manager"],
        task_repo=task_repo,
        team_inbox=inbox,
    )
    assert out == {"disabled_jobs": 1, "drained_jobs": 1, "cancelled_tasks": 2}
    assert env["cron_manager"].list_by_agent(env["team_id"]) == []
    assert [t.status for t in task_repo.list_by_team(env["team_id"])] == [
        "cancelled",
        "cancelled",
        "done",
    ]


@pytest.mark.asyncio
async def test_non_creator_cannot_use_drive_tools(env: dict[str, Any]) -> None:
    tools = _tools(env)
    denied = await _call(env, env["bob"], tools["drive_enable"], trigger="interval:3600")
    assert "error" in denied
    assert "creator" in denied["error"]


def _seed_team(db: SqlitePool, team_id: str = "t1") -> None:
    user = UserRepo(db).create(username="alice", password_hash="h", role="user")
    repo = AgentRepo(db)
    repo.create(agent_id=team_id, user_id=user, name="crew", kind="team")
    repo.create(agent_id="m1", user_id=user, name="worker-1")
    repo.create(agent_id="m2", user_id=user, name="worker-2")


def test_reap_stale_tasks_marks_lost_running_failed(db: SqlitePool) -> None:
    _seed_team(db)
    repo = TeamTaskRepo(db)
    repo.create(team_id="t1", title="old", status="running", assignee="m1")
    repo.create(team_id="t1", title="busy", status="running", assignee="m2")
    repo.create(team_id="t1", title="done", status="done")

    jobs = MagicMock()
    jobs.busy_member_ids = MagicMock(return_value={"m2"})
    reaped = reap_stale_tasks(team_id="t1", task_repo=repo, jobs=jobs, boot_ts=9_999_999_999)
    assert len(reaped) == 1
    stale = [t for t in repo.list_by_team("t1") if t.status == "failed"]
    assert len(stale) == 1
    assert stale[0].title == "old"
    assert "stale" in stale[0].result_summary
    assert [t.status for t in repo.list_by_team("t1") if t.title == "busy"] == ["running"]


def test_reap_skips_tasks_touched_after_boot(db: SqlitePool) -> None:
    _seed_team(db)
    repo = TeamTaskRepo(db)
    task = repo.create(team_id="t1", title="fresh", status="running", assignee="m1")
    # updated_at is now; a boot_ts in the past must not reap live work.
    assert reap_stale_tasks(team_id="t1", task_repo=repo, jobs=None, boot_ts=0) == []
    fresh = repo.get(task.task_id)
    assert fresh is not None and fresh.status == "running"


@pytest.mark.asyncio
async def test_agent_delete_cascades_drive_job(services: Any) -> None:
    """Deleting the team removes cron rows the v1 loop left behind."""
    manager = AgentManager(repos=services.repos, paths=services.paths)
    manager.set_cron_manager(_cron_manager(services))
    harness = MagicMock()
    harness.aremove_agent = AsyncMock()
    manager._harness_manager = harness  # type: ignore[assignment]

    repos = services.repos
    alice = repos.user_repo.create(username="carol", password_hash="h", role="user")
    repos.agent_repo.create(agent_id="team-del", user_id=alice, name="crew", kind="team")
    repos.cron_repo.create(
        cron_id="drive-team-del",
        agent_id="team-del",
        user_id=alice,
        trigger="interval:900",
        prompt="legacy drive wake",
        session_key=f"team-del:dashboard:{alice}:dm",
        meta={"kind": "team_drive"},
    )

    with patch("octop.infra.agents.memory.thread_cleanup.delete_agent_memory_and_checkpoints"):
        await manager.delete("team-del")

    assert repos.cron_repo.get("drive-team-del") is None
    assert repos.agent_repo.get("team-del") is None


class _FakeAutopilot:
    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []
        self.stopped = 0

    async def start(self, team_id: str, **kwargs: Any) -> None:
        self.started.append({"team_id": team_id, **kwargs})

    async def stop(self, team_id: str, **kwargs: Any) -> dict[str, int]:
        self.stopped += 1
        return {"disabled_jobs": 0, "drained_jobs": 0, "cancelled_tasks": 1, "cancelled_waits": 1}

    def status_payload(self, team_id: str) -> dict[str, Any]:
        return {"team_id": team_id, "status": "running", "mode": "autopilot", "open_waits": []}


@pytest.mark.asyncio
async def test_drive_tools_use_autopilot_when_attached(env: dict[str, Any]) -> None:
    autopilot = _FakeAutopilot()
    tools = _tools(env, autopilot=autopilot)

    out = await _call(env, env["alice"], tools["drive_enable"], trigger="interval:3600")
    assert out["enabled"] is True
    assert out["status"] == "running"
    assert autopilot.started == [
        {"team_id": env["team_id"], "user_id": env["alice"], "trigger": "interval:3600"}
    ]
    assert env["cron_manager"].list_by_agent(env["team_id"]) == []

    listed = await _call(env, env["alice"], tools["drive_list"])
    assert listed["enabled"] is True and listed["status"] == "running"

    stopped = await _call(env, env["alice"], tools["drive_disable"])
    assert stopped["stopped"] is True and autopilot.stopped == 1
