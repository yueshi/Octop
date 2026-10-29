"""Integration tests for the agent-generic board task API.

``/agents/{id}/tasks`` is the surface a digital employee is managed through.
It shares its validation and CAS guard with ``/teams/{id}/tasks`` — one
implementation, so the server cannot disagree with itself about the state
machine.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from octop.infra.server import OctopServer

Env = tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str]


@pytest.fixture
async def solo(env_alice_bob_agent: Env) -> tuple[Any, str, str]:
    """An alice-owned expert flagged as a digital employee.

    The flag is written directly rather than through ``PUT /autonomy``: that
    route starts the autopilot loop, and a live loop would run the task the
    test is about to create.
    """
    client, srv, alice_auth, _bob_auth, agent_id = env_alice_bob_agent
    assert srv.services is not None
    srv.services.agent_repo.set_autonomy_enabled(agent_id, True)
    return client, alice_auth, agent_id


async def test_create_list_and_patch_a_task(
    solo: tuple[Any, str, str],
) -> None:
    client, alice_auth, agent_id = solo

    r = await client.post(
        f"/api/agents/{agent_id}/tasks",
        headers=alice_auth,
        json={"title": "Draft the report", "spec": "two pages", "assignee": agent_id},
    )
    assert r.status_code == 201, r.text
    task = r.json()
    assert task["status"] == "proposed"
    assert task["assignee"] == agent_id
    assert task["version"] == 1

    r = await client.get(f"/api/agents/{agent_id}/tasks", headers=alice_auth)
    assert r.status_code == 200
    assert [t["task_id"] for t in r.json()] == [task["task_id"]]

    r = await client.patch(
        f"/api/agents/{agent_id}/tasks/{task['task_id']}",
        headers=alice_auth,
        json={"status": "running", "expected_version": task["version"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "running"
    assert r.json()["version"] == task["version"] + 1


async def test_unassigned_task_defaults_to_the_agent_itself(
    solo: tuple[Any, str, str],
) -> None:
    """The API defaults an unassigned row to the employee, like the tool does.

    Without this the row would be escalated as "no assignee" and never run —
    and the validation must check the *resolved* assignee, not the request
    field, or the default path 404s on itself.
    """
    client, alice_auth, agent_id = solo

    r = await client.post(f"/api/agents/{agent_id}/tasks", headers=alice_auth, json={"title": "T"})

    assert r.status_code == 201, r.text
    assert r.json()["assignee"] == agent_id


async def test_autonomous_agent_cannot_assign_a_stranger(
    solo: tuple[Any, str, str],
    env_alice_bob_agent: Env,
) -> None:
    client, alice_auth, agent_id = solo
    _c, srv, _alice, _bob, _same = env_alice_bob_agent
    assert srv.services is not None
    alice = srv.services.user_repo.get_by_username("alice")
    assert alice is not None
    srv.services.agent_repo.create(agent_id="stranger-api", user_id=alice.id, name="someone else")

    r = await client.post(
        f"/api/agents/{agent_id}/tasks",
        headers=alice_auth,
        json={"title": "T", "assignee": "stranger-api"},
    )

    assert r.status_code == 400
    assert r.json()["error"]["code"] == "TEAM_TASK_ASSIGNEE_INVALID"


async def test_illegal_transition_is_rejected(
    env_alice_bob_agent: Env,
) -> None:
    client, _srv, alice_auth, _bob, agent_id = env_alice_bob_agent
    r = await client.post(f"/api/agents/{agent_id}/tasks", headers=alice_auth, json={"title": "T"})
    print("DEBUG_POST", r.status_code, r.text[:300])
    task = r.json()

    # ``proposed -> review`` is not an edge in the shared state machine.
    r = await client.patch(
        f"/api/agents/{agent_id}/tasks/{task['task_id']}",
        headers=alice_auth,
        json={"status": "review", "expected_version": task["version"]},
    )

    assert r.status_code == 400
    assert r.json()["error"]["code"] == "TEAM_TASK_INVALID_TRANSITION"


async def test_stale_version_conflicts(
    env_alice_bob_agent: Env,
) -> None:
    client, _srv, alice_auth, _bob, agent_id = env_alice_bob_agent
    r = await client.post(f"/api/agents/{agent_id}/tasks", headers=alice_auth, json={"title": "T"})
    task = r.json()

    r = await client.patch(
        f"/api/agents/{agent_id}/tasks/{task['task_id']}",
        headers=alice_auth,
        json={"status": "running", "expected_version": task["version"] + 99},
    )

    assert r.status_code == 409
    assert r.json()["error"]["code"] == "TEAM_TASK_CONFLICT"


async def test_other_user_cannot_touch_the_board(
    solo: tuple[Any, str, str],
    env_alice_bob_agent: Env,
) -> None:
    client, _alice_auth, agent_id = solo
    _c, _srv, _alice, bob_auth, _same = env_alice_bob_agent

    r = await client.get(f"/api/agents/{agent_id}/tasks", headers=bob_auth)
    assert r.status_code == 403

    r = await client.post(f"/api/agents/{agent_id}/tasks", headers=bob_auth, json={"title": "T"})
    assert r.status_code == 403


async def test_team_task_route_returns_the_same_shape(
    env_alice_bob_agent: Env,
) -> None:
    """``/teams`` and ``/agents`` must serialize a task identically."""
    client, srv, alice_auth, _bob_auth, _agent = env_alice_bob_agent
    assert srv.services is not None
    alice = srv.services.user_repo.get_by_username("alice")
    assert alice is not None
    srv.services.agent_repo.create(
        agent_id="crew-shape", user_id=alice.id, name="crew", kind="team"
    )
    task = srv.services.team_task_repo.create(team_id="crew-shape", title="T")

    r = await client.get("/api/teams/crew-shape/tasks", headers=alice_auth)
    team_shape = r.json()[0]

    with srv.services.db.transaction() as conn:
        conn.execute("UPDATE agents SET autonomy_enabled = 1 WHERE agent_id = ?", ("crew-shape",))
    r = await client.get("/api/agents/crew-shape/tasks", headers=alice_auth)
    agent_shape = r.json()[0]

    assert set(team_shape) == set(agent_shape)
    assert team_shape["task_id"] == task.task_id == agent_shape["task_id"]


async def test_review_approval_closes_the_task_and_records_who(
    solo: tuple[Any, str, str],
    env_alice_bob_agent: Env,
) -> None:
    """``review`` used to be a bare status: no reviewer, no opinion, no evidence."""
    client, alice_auth, agent_id = solo
    _c, srv, _alice, _bob, _same = env_alice_bob_agent
    assert srv.services is not None
    task = srv.services.team_task_repo.create(team_id=agent_id, title="Ship it")
    repo = srv.services.team_task_repo
    for target in ("running", "review"):
        row = repo.get(task.task_id)
        assert row is not None
        repo.update(task.task_id, expected_version=row.version, fields={"status": target})

    current = repo.get(task.task_id)
    assert current is not None
    r = await client.post(
        f"/api/agents/{agent_id}/tasks/{task.task_id}/review",
        headers=alice_auth,
        json={
            "decision": "approved",
            "comment": "checked the numbers",
            "expected_version": current.version,
        },
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task"]["status"] == "done"
    assert body["review"]["decision"] == "approved"
    assert body["review"]["comment"] == "checked the numbers"
    assert body["review"]["reviewer"].startswith("user:")

    r = await client.get(f"/api/agents/{agent_id}/tasks/{task.task_id}/reviews", headers=alice_auth)
    assert [item["decision"] for item in r.json()] == ["approved"]


async def test_review_rejection_sends_the_task_back(
    solo: tuple[Any, str, str],
    env_alice_bob_agent: Env,
) -> None:
    client, alice_auth, agent_id = solo
    _c, srv, _alice, _bob, _same = env_alice_bob_agent
    assert srv.services is not None
    repo = srv.services.team_task_repo
    task = repo.create(team_id=agent_id, title="Ship it")
    for target in ("running", "review"):
        row = repo.get(task.task_id)
        assert row is not None
        repo.update(task.task_id, expected_version=row.version, fields={"status": target})

    current = repo.get(task.task_id)
    assert current is not None
    r = await client.post(
        f"/api/agents/{agent_id}/tasks/{task.task_id}/review",
        headers=alice_auth,
        json={
            "decision": "rejected",
            "comment": "missing the appendix",
            "expected_version": current.version,
        },
    )

    assert r.status_code == 200, r.text
    assert r.json()["task"]["status"] == "proposed"


async def test_review_requires_the_review_status(
    env_alice_bob_agent: Env,
) -> None:
    client, _srv, alice_auth, _bob, agent_id = env_alice_bob_agent
    r = await client.post(f"/api/agents/{agent_id}/tasks", headers=alice_auth, json={"title": "T"})
    task = r.json()

    r = await client.post(
        f"/api/agents/{agent_id}/tasks/{task['task_id']}/review",
        headers=alice_auth,
        json={"decision": "approved", "expected_version": task["version"]},
    )

    assert r.status_code == 400
    assert r.json()["error"]["code"] == "TEAM_TASK_INVALID_TRANSITION"
