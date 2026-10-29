"""Goal + deliverable API for experts and teams."""

from __future__ import annotations

import httpx

from octop.infra.server import OctopServer


async def test_expert_goal_with_deliverables_and_artifact(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, _srv, alice_auth, _bob_auth, agent_id = env_alice_bob_agent

    r = await client.post(
        f"/api/agents/{agent_id}/goals",
        headers=alice_auth,
        json={
            "title": "季度经营复盘",
            "description": "基于门店数据输出复盘",
            "success_criteria": "老板可直接阅读",
            "deliverables": [
                {"title": "复盘报告", "format": "docx", "required": True},
                {"title": "数据底稿", "required": False},
            ],
        },
    )
    assert r.status_code == 200, r.text
    goal = r.json()
    assert goal["agent_id"] == agent_id
    assert [d["title"] for d in goal["deliverables"]] == ["复盘报告", "数据底稿"]
    assert all(d["status"] == "pending" for d in goal["deliverables"])

    deliverable_id = goal["deliverables"][0]["deliverable_id"]
    r = await client.patch(
        f"/api/agents/{agent_id}/goals/{goal['goal_id']}/deliverables/{deliverable_id}",
        headers=alice_auth,
        json={
            "status": "produced",
            "artifact_type": "file",
            "artifact_ref": "reports/quarter-review.docx",
            "artifact_name": "季度复盘.docx",
        },
    )
    assert r.status_code == 200, r.text
    updated = r.json()
    assert updated["status"] == "produced"
    assert updated["artifact_type"] == "file"
    assert updated["artifact_agent_id"] == agent_id

    r = await client.get(f"/api/agents/{agent_id}/goals", headers=alice_auth)
    assert r.status_code == 200
    assert r.json()[0]["deliverables"][0]["artifact_ref"] == "reports/quarter-review.docx"


async def test_team_goal_updates_runtime_and_ledger(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, srv, alice_auth, _bob_auth, _agent_id = env_alice_bob_agent
    assert srv.services is not None
    alice = srv.services.user_repo.get_by_username("alice")
    assert alice is not None
    srv.services.agent_repo.create(
        agent_id="goal-team-01", user_id=alice.id, name="goal team", kind="team"
    )

    r = await client.post(
        "/api/agents/goal-team-01/goals",
        headers=alice_auth,
        json={
            "title": "交付门店方案",
            "deliverables": [{"title": "方案文档"}, {"title": "演示单页"}],
        },
    )
    assert r.status_code == 200, r.text
    goal = r.json()
    assert goal["team_id"] == "goal-team-01"
    assert len(goal["deliverables"]) == 2

    runtime = srv.services.team_runtime_repo.get("goal-team-01")
    assert runtime is not None and runtime.goal_id == goal["goal_id"]
    events = srv.services.team_event_repo.list_after("goal-team-01")
    assert any(e.type == "goal.changed" for e in events)


async def test_goals_reject_non_owner_and_bad_artifacts(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, _srv, alice_auth, bob_auth, agent_id = env_alice_bob_agent

    r = await client.post(
        f"/api/agents/{agent_id}/goals", headers=bob_auth, json={"title": "hijack"}
    )
    assert r.status_code == 403

    r = await client.post(
        f"/api/agents/{agent_id}/goals",
        headers=alice_auth,
        json={"title": "valid", "deliverables": [{"title": "产物"}]},
    )
    assert r.status_code == 200
    goal = r.json()
    deliverable_id = goal["deliverables"][0]["deliverable_id"]
    path = f"/api/agents/{agent_id}/goals/{goal['goal_id']}/deliverables/{deliverable_id}"

    r = await client.patch(path, headers=alice_auth, json={"status": "produced"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GOAL_DELIVERABLE_INVALID"

    r = await client.patch(
        path,
        headers=alice_auth,
        json={"artifact_type": "link", "artifact_ref": "ftp://example.com/x"},
    )
    assert r.status_code == 400

    r = await client.patch(
        path,
        headers=alice_auth,
        json={"artifact_type": "file", "artifact_ref": "../../etc/passwd"},
    )
    assert r.status_code == 400
