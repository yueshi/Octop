"""Integration tests for the digital-employee switch and job spec."""

from __future__ import annotations

import httpx

from octop.infra.server import OctopServer

Env = tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str]


async def test_switch_hires_and_dismisses_the_agent(
    env_alice_bob_agent: Env,
) -> None:
    client, srv, alice_auth, _bob_auth, agent_id = env_alice_bob_agent
    assert srv.services is not None

    r = await client.put(
        f"/api/agents/{agent_id}/autonomy", headers=alice_auth, json={"enabled": True}
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"agent_id": agent_id, "enabled": True}
    assert srv.services.agent_repo.get(agent_id).autonomy_enabled == 1  # type: ignore[union-attr]

    r = await client.put(
        f"/api/agents/{agent_id}/autonomy", headers=alice_auth, json={"enabled": False}
    )
    assert r.status_code == 200
    assert srv.services.agent_repo.get(agent_id).autonomy_enabled == 0  # type: ignore[union-attr]


async def test_team_host_uses_its_own_switch(
    env_alice_bob_agent: Env,
) -> None:
    """Setting agent autonomy on a team host is ambiguous, so it is refused."""
    client, srv, alice_auth, _bob, _agent = env_alice_bob_agent
    assert srv.services is not None
    alice = srv.services.user_repo.get_by_username("alice")
    assert alice is not None
    srv.services.agent_repo.create(
        agent_id="crew-switch", user_id=alice.id, name="crew", kind="team"
    )

    r = await client.put(
        "/api/agents/crew-switch/autonomy", headers=alice_auth, json={"enabled": True}
    )

    assert r.status_code == 400


async def test_policy_round_trips(
    env_alice_bob_agent: Env,
) -> None:
    client, _srv, alice_auth, _bob, agent_id = env_alice_bob_agent

    r = await client.get(f"/api/agents/{agent_id}/autonomy-policy", headers=alice_auth)
    assert r.status_code == 200
    assert r.json()["budget_limit_tokens"] == 0  # defaults for an unconfigured agent

    r = await client.put(
        f"/api/agents/{agent_id}/autonomy-policy",
        headers=alice_auth,
        json={
            "active_hours_start": "08:30",
            "active_hours_end": "20:00",
            "min_interval_hours": 2,
            "max_interval_hours": 8,
            "budget_limit_tokens": 500_000,
            "budget_window": "month",
            "sla_tasks_per_day": 3,
            "escalate_to": "user:1",
            "responsible_scope": "Weekly reporting",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["budget_limit_tokens"] == 500_000
    assert body["responsible_scope"] == "Weekly reporting"

    r = await client.get(f"/api/agents/{agent_id}/autonomy-policy", headers=alice_auth)
    assert r.json()["budget_window"] == "month"
    assert r.json()["sla_tasks_per_day"] == 3


async def test_policy_rejects_an_inverted_window(
    env_alice_bob_agent: Env,
) -> None:
    client, _srv, alice_auth, _bob, agent_id = env_alice_bob_agent

    r = await client.put(
        f"/api/agents/{agent_id}/autonomy-policy",
        headers=alice_auth,
        json={"active_hours_start": "20:00", "active_hours_end": "08:00"},
    )

    assert r.status_code == 422


async def test_policy_is_owner_only(
    env_alice_bob_agent: Env,
) -> None:
    client, _srv, _alice_auth, bob_auth, agent_id = env_alice_bob_agent

    r = await client.get(f"/api/agents/{agent_id}/autonomy-policy", headers=bob_auth)

    assert r.status_code == 403


async def test_inbox_lists_what_needs_a_human(
    env_alice_bob_agent: Env,
) -> None:
    """One place to answer "does this employee need me right now"."""
    client, srv, alice_auth, _bob, agent_id = env_alice_bob_agent
    assert srv.services is not None
    repo = srv.services.team_task_repo
    failed = repo.create(team_id=agent_id, title="broken", status="failed")
    overdue = repo.create(team_id=agent_id, title="late", due_at=1)
    repo.create(team_id=agent_id, title="fine")

    r = await client.get(f"/api/agents/{agent_id}/inbox", headers=alice_auth)

    assert r.status_code == 200, r.text
    body = r.json()
    kinds = {item["kind"] for item in body["items"]}
    assert "failed" in kinds
    assert "overdue" in kinds
    ids = {item["task_id"] for item in body["items"] if item["task_id"]}
    assert {failed.task_id, overdue.task_id} <= ids
    assert body["counts"]["failed"] == 1


async def test_inbox_reports_an_exhausted_budget(
    env_alice_bob_agent: Env,
) -> None:
    from octop.infra.db.repos.autonomy_policy import AutonomyPolicy

    client, srv, alice_auth, _bob, agent_id = env_alice_bob_agent
    assert srv.services is not None
    srv.services.autonomy_policy_repo.upsert(
        AutonomyPolicy(agent_id=agent_id, budget_limit_tokens=100)
    )
    srv.services.team_runtime_repo.ensure(agent_id)
    srv.services.team_runtime_repo.add_budget_used(agent_id, 100)

    r = await client.get(f"/api/agents/{agent_id}/inbox", headers=alice_auth)

    assert "budget" in {item["kind"] for item in r.json()["items"]}


async def test_metrics_count_tasks_and_report_budget(
    env_alice_bob_agent: Env,
) -> None:
    from octop.infra.db.repos.autonomy_policy import AutonomyPolicy

    client, srv, alice_auth, _bob, agent_id = env_alice_bob_agent
    assert srv.services is not None
    repo = srv.services.team_task_repo
    repo.create(team_id=agent_id, title="a", status="done")
    repo.create(team_id=agent_id, title="b", status="done")
    repo.create(team_id=agent_id, title="c")
    srv.services.autonomy_policy_repo.upsert(
        AutonomyPolicy(agent_id=agent_id, budget_limit_tokens=1000)
    )
    srv.services.team_runtime_repo.ensure(agent_id)
    srv.services.team_runtime_repo.add_budget_used(agent_id, 250)
    alice = srv.services.user_repo.get_by_username("alice")
    assert alice is not None
    srv.services.usage_repo.record(
        agent_id=agent_id,
        user_id=alice.id,
        input_tokens=100,
        output_tokens=40,
    )

    r = await client.get(f"/api/agents/{agent_id}/metrics", headers=alice_auth)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tasks"]["done"] == 2
    assert body["tasks"]["open"] == 1
    assert body["budget"]["used"] == 250
    assert body["budget"]["remaining"] == 750
    assert body["usage"]["total_tokens"] == 140
    assert body["usage"]["input_tokens"] == 100


async def test_roster_lists_only_digital_employees(
    env_alice_bob_agent: Env,
) -> None:
    """``?autonomy=true`` answers "who is on duty right now"."""
    client, srv, alice_auth, _bob, agent_id = env_alice_bob_agent
    assert srv.services is not None
    # A second, not-hired agent so the filter has something to exclude.
    other = await client.post(
        "/api/agents",
        headers=alice_auth,
        json={"name": "plain-colleague", "config": {"providers": []}},
    )
    assert other.status_code == 201, other.text
    other_id = other.json()["agent_id"]

    r = await client.put(
        f"/api/agents/{agent_id}/autonomy", headers=alice_auth, json={"enabled": True}
    )
    assert r.status_code == 200

    r = await client.get("/api/agents?autonomy=true", headers=alice_auth)
    assert r.status_code == 200, r.text
    ids = [row["agent_id"] for row in r.json()]
    assert ids == [agent_id]
    assert other_id not in ids
    assert r.json()[0]["autonomy_enabled"] is True

    r = await client.get("/api/agents?autonomy=false", headers=alice_auth)
    plain_ids = [row["agent_id"] for row in r.json()]
    assert agent_id not in plain_ids
    assert other_id in plain_ids
