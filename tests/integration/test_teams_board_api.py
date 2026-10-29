"""Integration tests for the team board API (goals / tasks, CAS + transitions)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from octop.infra.server import OctopServer


@pytest.fixture
async def team_id(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> str:
    _client, srv, alice_auth, _bob_auth, _agent_id = env_alice_bob_agent
    assert srv.services is not None
    alice_id = srv.services.user_repo.get_by_username("alice")
    assert alice_id is not None
    srv.services.agent_repo.create(
        agent_id="team-board-01", user_id=alice_id.id, name="board crew", kind="team"
    )
    return "team-board-01"


async def test_goal_and_task_flow_with_cas(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
    team_id: str,
) -> None:
    client, srv, alice_auth, _bob_auth, _agent_id = env_alice_bob_agent
    assert srv.services is not None

    r = await client.get(f"/api/teams/{team_id}/goals", headers=alice_auth)
    assert r.status_code == 200
    assert r.json() == []

    r = await client.post(
        f"/api/teams/{team_id}/goals",
        headers=alice_auth,
        json={"title": "Ship autopilot", "success_criteria": "board live"},
    )
    assert r.status_code == 200
    goal = r.json()
    assert goal["status"] == "active"

    r = await client.get(f"/api/teams/{team_id}/goals", headers=alice_auth)
    assert [g["goal_id"] for g in r.json()] == [goal["goal_id"]]

    task_repo = srv.services.team_task_repo
    task = task_repo.create(team_id=team_id, title="Draft spec", goal_id=goal["goal_id"])

    r = await client.get(f"/api/teams/{team_id}/tasks", headers=alice_auth)
    assert [t["task_id"] for t in r.json()] == [task.task_id]

    # CAS bump on the current version.
    r = await client.patch(
        f"/api/teams/{team_id}/tasks/{task.task_id}",
        headers=alice_auth,
        json={"status": "running", "expected_version": task.version},
    )
    assert r.status_code == 200
    assert r.json()["version"] == task.version + 1

    # Stale version → 409.
    r = await client.patch(
        f"/api/teams/{team_id}/tasks/{task.task_id}",
        headers=alice_auth,
        json={"status": "done", "expected_version": task.version},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "TEAM_TASK_CONFLICT"

    # Illegal transition running -> proposed is legal; use a truly illegal one
    # (done -> running) after closing the task.
    r = await client.patch(
        f"/api/teams/{team_id}/tasks/{task.task_id}",
        headers=alice_auth,
        json={"status": "done", "expected_version": task.version + 1},
    )
    assert r.status_code == 200
    r = await client.patch(
        f"/api/teams/{team_id}/tasks/{task.task_id}",
        headers=alice_auth,
        json={"status": "running", "expected_version": task.version + 2},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "TEAM_TASK_INVALID_TRANSITION"


async def test_board_endpoints_reject_non_owner(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
    team_id: str,
) -> None:
    client, _srv, _alice_auth, bob_auth, _agent_id = env_alice_bob_agent
    r = await client.get(f"/api/teams/{team_id}/goals", headers=bob_auth)
    assert r.status_code == 403
    r = await client.post(f"/api/teams/{team_id}/goals", headers=bob_auth, json={"title": "hijack"})
    assert r.status_code == 403
    r = await client.patch(
        f"/api/teams/{team_id}/tasks/t1", headers=bob_auth, json={"expected_version": 1}
    )
    assert r.status_code == 403


async def test_drive_stop_cancels_open_tasks(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
    team_id: str,
) -> None:
    client, srv, alice_auth, bob_auth, _agent_id = env_alice_bob_agent
    assert srv.services is not None
    task_repo = srv.services.team_task_repo
    task_repo.create(team_id=team_id, title="t-running", status="running")
    task_repo.create(team_id=team_id, title="t-proposed")
    task_repo.create(team_id=team_id, title="t-done", status="done")

    r = await client.post(f"/api/teams/{team_id}/drive/stop", headers=alice_auth)
    assert r.status_code == 200
    body = r.json()
    assert body["cancelled_tasks"] == 2
    statuses = {t.title: t.status for t in task_repo.list_by_team(team_id)}
    assert statuses == {"t-running": "cancelled", "t-proposed": "cancelled", "t-done": "done"}

    # Idempotent: a second stop closes nothing new.
    r = await client.post(f"/api/teams/{team_id}/drive/stop", headers=alice_auth)
    assert r.status_code == 200
    assert r.json()["cancelled_tasks"] == 0


async def test_drive_stop_rejects_non_owner(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
    team_id: str,
) -> None:
    client, _srv, _alice_auth, bob_auth, _agent_id = env_alice_bob_agent
    r = await client.post(f"/api/teams/{team_id}/drive/stop", headers=bob_auth)
    assert r.status_code == 403


async def test_patch_task_emits_board_event_to_owner(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
    team_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, srv, alice_auth, _bob_auth, _agent_id = env_alice_bob_agent
    assert srv.services is not None and srv.app_runtime is not None
    task_repo = srv.services.team_task_repo
    task = task_repo.create(team_id=team_id, title="t1")

    pushed: list[tuple[int, dict[str, Any]]] = []

    async def _record(user_id: int, frame: dict[str, Any]) -> None:
        pushed.append((user_id, frame))

    monkeypatch.setattr(srv.app_runtime.gateway.ws_hub, "push_to_user", _record)

    r = await client.patch(
        f"/api/teams/{team_id}/tasks/{task.task_id}",
        headers=alice_auth,
        json={"status": "running", "expected_version": task.version},
    )
    assert r.status_code == 200
    alice = srv.services.user_repo.get_by_username("alice")
    assert alice is not None
    assert pushed == [
        (
            alice.id,
            {
                "type": "team.task.updated",
                "team_id": team_id,
                "task_id": task.task_id,
                "status": "running",
                "version": task.version + 1,
            },
        )
    ]

    # A rejected write (409 CAS) emits nothing.
    r = await client.patch(
        f"/api/teams/{team_id}/tasks/{task.task_id}",
        headers=alice_auth,
        json={"status": "done", "expected_version": task.version},
    )
    assert r.status_code == 409
    assert len(pushed) == 1


async def test_board_api_writes_append_shadow_events(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
    team_id: str,
) -> None:
    """Dashboard board writes land one shadow-ledger event each (v2 §11.2)."""
    client, srv, alice_auth, _bob_auth, _agent_id = env_alice_bob_agent
    assert srv.services is not None

    r = await client.post(
        f"/api/teams/{team_id}/goals",
        headers=alice_auth,
        json={"title": "Ledger goal", "success_criteria": "events recorded"},
    )
    assert r.status_code == 200

    task = srv.services.team_task_repo.create(team_id=team_id, title="Ledger task")
    r = await client.patch(
        f"/api/teams/{team_id}/tasks/{task.task_id}",
        headers=alice_auth,
        json={"status": "running", "expected_version": task.version},
    )
    assert r.status_code == 200

    alice = srv.services.user_repo.get_by_username("alice")
    assert alice is not None
    events = srv.services.team_event_repo.list_after(team_id)
    assert [(e.type, e.actor) for e in events] == [
        ("goal.changed", f"user:{alice.id}"),
        ("task.changed", f"user:{alice.id}"),
    ]
    assert events[1].payload["status"] == "running"
    runtime = srv.services.team_runtime_repo.get(team_id)
    assert runtime is not None and runtime.status == "idle"


async def test_runtime_drive_controls(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
    team_id: str,
) -> None:
    client, _srv, alice_auth, _bob_auth, _agent_id = env_alice_bob_agent

    r = await client.get(f"/api/teams/{team_id}/runtime", headers=alice_auth)
    assert r.status_code == 200
    assert r.json()["status"] == "idle"

    r = await client.post(f"/api/teams/{team_id}/drive/start", headers=alice_auth, json={})
    assert r.status_code == 200
    assert r.json()["status"] == "running"
    assert r.json()["mode"] == "autopilot"

    r = await client.post(
        f"/api/teams/{team_id}/drive/pause", headers=alice_auth, json={"reason": "lunch"}
    )
    assert r.status_code == 200
    assert r.json()["status"] == "paused_user"
    assert r.json()["paused_reason"] == "lunch"

    r = await client.post(f"/api/teams/{team_id}/drive/resume", headers=alice_auth)
    assert r.status_code == 200
    assert r.json()["status"] == "running"

    r = await client.post(f"/api/teams/{team_id}/drive/finish", headers=alice_auth)
    assert r.status_code == 200
    assert r.json()["status"] == "finishing"

    r = await client.get(f"/api/teams/{team_id}/decisions", headers=alice_auth)
    assert r.status_code == 200 and r.json() == []
    r = await client.get(f"/api/teams/{team_id}/waits", headers=alice_auth)
    assert r.status_code == 200 and r.json() == []

    r = await client.post(f"/api/teams/{team_id}/drive/stop", headers=alice_auth)
    assert r.status_code == 200
    assert r.json()["cancelled_waits"] == 0
    r = await client.get(f"/api/teams/{team_id}/runtime", headers=alice_auth)
    assert r.json()["status"] == "stopped"


async def test_runtime_endpoints_reject_non_owner(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
    team_id: str,
) -> None:
    client, _srv, _alice_auth, bob_auth, _agent_id = env_alice_bob_agent
    for method, path in (
        ("GET", f"/api/teams/{team_id}/runtime"),
        ("GET", f"/api/teams/{team_id}/decisions"),
        ("GET", f"/api/teams/{team_id}/waits"),
    ):
        r = await client.request(method, path, headers=bob_auth)
        assert r.status_code == 403
    r = await client.post(f"/api/teams/{team_id}/drive/start", headers=bob_auth, json={})
    assert r.status_code == 403


def _seed_approval_wait(
    srv: OctopServer,
    team_id: str,
    *,
    task_status: str = "needs_input",
    external_ref: str | None = None,
    with_task: bool = True,
) -> tuple[Any, Any]:
    from octop.infra.utils.ulid import new_short_id  # noqa: PLC0415

    assert srv.services is not None
    repos = srv.services.repos
    task = (
        repos.team_task_repo.create(team_id=team_id, title="Approve me", status=task_status)
        if with_task
        else None
    )
    wait = repos.team_wait_repo.create(
        team_id,
        kind="approval",
        idempotency_key=f"approval:{new_short_id()}",
        task_id=task.task_id if task is not None else None,
        external_ref=external_ref,
        timeout_at=1_900_000_000,
        payload={"reason": "spend budget"},
    )
    assert wait is not None
    if task is not None:
        repos.team_task_repo.update(
            task.task_id,
            expected_version=task.version,
            fields={
                "active_wait_id": wait.wait_id,
                "blocked_reason": "waiting for creator approval",
            },
        )
    return task, wait


async def test_resolve_plan_approval_wait_approved(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
    team_id: str,
) -> None:
    client, srv, alice_auth, _bob_auth, _agent_id = env_alice_bob_agent
    assert srv.services is not None
    task, wait = _seed_approval_wait(srv, team_id)
    assert task is not None

    runtime = await client.get(f"/api/teams/{team_id}/runtime", headers=alice_auth)
    pending = runtime.json()["open_waits"]
    assert [(w["wait_id"], w["external_ref"], w["reason"]) for w in pending] == [
        (wait.wait_id, None, "spend budget")
    ]

    r = await client.post(
        f"/api/teams/{team_id}/waits/{wait.wait_id}/resolve",
        headers=alice_auth,
        json={"status": "approved"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["wait"]["status"] == "settled"
    assert body["task"]["status"] == "proposed"
    assert body["task"]["active_wait_id"] is None

    updated = srv.services.team_task_repo.get(task.task_id)
    assert updated is not None and updated.status == "proposed"
    events = srv.services.team_event_repo.list_after(team_id)
    assert any(e.type == "approval.resolved" and e.payload["status"] == "approved" for e in events)


async def test_resolve_plan_approval_wait_rejected(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
    team_id: str,
) -> None:
    client, srv, alice_auth, _bob_auth, _agent_id = env_alice_bob_agent
    assert srv.services is not None
    task, wait = _seed_approval_wait(srv, team_id)
    assert task is not None

    r = await client.post(
        f"/api/teams/{team_id}/waits/{wait.wait_id}/resolve",
        headers=alice_auth,
        json={"status": "rejected", "note": "too costly"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["wait"]["status"] == "cancelled"
    assert body["task"]["status"] == "blocked"
    assert body["task"]["blocked_reason"] == "too costly"


async def test_resolve_team_level_approval_wait(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
    team_id: str,
) -> None:
    client, srv, alice_auth, _bob_auth, _agent_id = env_alice_bob_agent
    assert srv.services is not None
    _task, wait = _seed_approval_wait(srv, team_id, with_task=False)

    r = await client.post(
        f"/api/teams/{team_id}/waits/{wait.wait_id}/resolve",
        headers=alice_auth,
        json={"status": "approved"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["task"] is None
    assert r.json()["wait"]["status"] == "settled"


async def test_resolve_wait_rejects_member_hitl_and_stale(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
    team_id: str,
) -> None:
    client, srv, alice_auth, _bob_auth, _agent_id = env_alice_bob_agent
    assert srv.services is not None
    _task, hitl = _seed_approval_wait(srv, team_id, external_ref="pending-1")

    r = await client.post(
        f"/api/teams/{team_id}/waits/{hitl.wait_id}/resolve",
        headers=alice_auth,
        json={"status": "approved"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "TEAM_WAIT_CONFLICT"

    _task2, stale = _seed_approval_wait(srv, team_id)
    assert srv.services.team_wait_repo.settle(stale.wait_id, status="timed_out")
    r = await client.post(
        f"/api/teams/{team_id}/waits/{stale.wait_id}/resolve",
        headers=alice_auth,
        json={"status": "approved"},
    )
    assert r.status_code == 409

    r = await client.post(
        f"/api/teams/{team_id}/waits/missing/resolve",
        headers=alice_auth,
        json={"status": "approved"},
    )
    assert r.status_code == 404


async def test_patch_cannot_bypass_open_approval(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
    team_id: str,
) -> None:
    client, srv, alice_auth, _bob_auth, _agent_id = env_alice_bob_agent
    assert srv.services is not None
    task, _wait = _seed_approval_wait(srv, team_id)
    assert task is not None
    current = srv.services.team_task_repo.get(task.task_id)
    assert current is not None

    r = await client.patch(
        f"/api/teams/{team_id}/tasks/{task.task_id}",
        headers=alice_auth,
        json={"status": "running", "expected_version": current.version},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "TEAM_WAIT_CONFLICT"
