"""External event plane: webhook ingress + management API."""

from __future__ import annotations

import asyncio
import json
import time

import httpx

from octop.infra.apps.service import AppService
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.connectors import ConnectorRepo
from octop.infra.events.security import (
    EVENT_ID_HEADER,
    EVENT_TYPE_HEADER,
    MAX_PAYLOAD_BYTES,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    sign,
)
from octop.infra.server import OctopServer

COLUMNS: list[dict[str, object]] = [
    {"name": "title", "type": "string", "required": True},
    {"name": "status", "type": "enum", "values": ["new", "done"]},
]


def _app_service(srv: OctopServer) -> AppService:
    assert srv.services is not None
    assert srv.app_runtime is not None
    return AppService(
        app_repo=srv.services.light_app_repo,
        agent_repo=srv.services.agent_repo,
        user_repo=srv.services.user_repo,
        workspaces=srv.app_runtime.agent_registry,
    )


async def _create_app(srv: OctopServer, agent_id: str, username: str) -> str:
    assert srv.services is not None
    owner = srv.services.user_repo.get_by_username(username)
    assert owner is not None
    row = await _app_service(srv).create(
        agent_id=agent_id,
        owner_user_id=owner.id,
        name="Tracker",
        description="external events",
        columns=COLUMNS,
    )
    return row.app_id


async def _create_source(client: httpx.AsyncClient, auth: dict[str, str]) -> tuple[str, str]:
    r = await client.post(
        "/api/external/sources",
        headers=auth,
        json={"name": "Orders", "kind": "webhook"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body["source_id"], body["secret"]


async def _post_webhook(
    client: httpx.AsyncClient,
    source_id: str,
    secret: str,
    *,
    external_id: str,
    payload: dict[str, object],
    timestamp: int | None = None,
    signature: str | None = None,
) -> httpx.Response:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    stamp = str(timestamp if timestamp is not None else int(time.time()))
    return await client.post(
        f"/api/hooks/{source_id}",
        content=body,
        headers={
            "Content-Type": "application/json",
            TIMESTAMP_HEADER: stamp,
            SIGNATURE_HEADER: signature if signature is not None else sign(secret, stamp, body),
            EVENT_ID_HEADER: external_id,
            EVENT_TYPE_HEADER: "order.created",
        },
    )


async def _stop_worker(srv: OctopServer) -> None:
    assert srv.app_runtime is not None
    worker = srv.app_runtime.external_event_worker
    assert worker is not None
    await worker.shutdown()


async def _drain_worker(srv: OctopServer) -> None:
    assert srv.app_runtime is not None
    worker = srv.app_runtime.external_event_worker
    assert worker is not None
    await worker.tick()
    await worker.drain()


async def test_webhook_to_app_import_and_dedupe(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, srv, alice_auth, _bob_auth, agent_id = env_alice_bob_agent
    await _stop_worker(srv)
    app_id = await _create_app(srv, agent_id, "alice")
    source_id, secret = await _create_source(client, alice_auth)

    r = await client.post(
        "/api/external/subscriptions",
        headers=alice_auth,
        json={
            "source_id": source_id,
            "target_type": "app",
            "target_id": app_id,
            "mode": "app.import",
            "mapping": {"table": "main", "columns": {"name": "title"}},
        },
    )
    assert r.status_code == 200, r.text
    subscription_id = r.json()["subscription_id"]
    assert r.json()["filter"] == {}

    payload = {
        "summary": "订单 #123 已创建",
        "records": [{"name": "A"}],
    }
    r = await _post_webhook(client, source_id, secret, external_id="evt-1", payload=payload)
    assert r.status_code == 202, r.text
    accepted = r.json()
    assert accepted["accepted"] is True
    assert accepted["duplicate"] is False
    assert accepted["deliveries"] == 1

    await _drain_worker(srv)

    rows = await client.get(f"/api/apps/{app_id}/rows?limit=10", headers=alice_auth)
    assert rows.status_code == 200
    assert rows.json()["total"] == 1
    assert rows.json()["rows"] == [{"title": "A", "status": ""}]

    r = await _post_webhook(client, source_id, secret, external_id="evt-1", payload=payload)
    assert r.status_code == 202
    assert r.json()["duplicate"] is True
    assert r.json()["deliveries"] == 0

    deliveries = await client.get(
        f"/api/external/deliveries?source_id={source_id}", headers=alice_auth
    )
    assert deliveries.status_code == 200
    assert len(deliveries.json()) == 1
    assert deliveries.json()[0]["status"] == "delivered"
    assert deliveries.json()[0]["subscription_id"] == subscription_id

    events = await client.get(f"/api/external/events?source_id={source_id}", headers=alice_auth)
    assert events.status_code == 200
    assert [item["external_id"] for item in events.json()] == ["evt-1"]


async def test_webhook_rejects_bad_signature_and_stale_timestamp(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, _srv, alice_auth, _bob_auth, _agent_id = env_alice_bob_agent
    source_id, secret = await _create_source(client, alice_auth)

    r = await _post_webhook(
        client,
        source_id,
        secret,
        external_id="evt-1",
        payload={"summary": "x"},
        signature="0" * 64,
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "EXTERNAL_SIGNATURE_INVALID"

    r = await _post_webhook(
        client,
        source_id,
        secret,
        external_id="evt-1",
        payload={"summary": "x"},
        timestamp=int(time.time()) - 301,
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "EXTERNAL_TIMESTAMP_INVALID"

    r = await client.post("/api/hooks/missing", content=b"{}")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "EXTERNAL_SOURCE_NOT_FOUND"

    r = await client.post(f"/api/hooks/{source_id}", content=b"x" * (MAX_PAYLOAD_BYTES + 1))
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "EXTERNAL_PAYLOAD_TOO_LARGE"


async def test_subscription_acl_and_management_crud(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, srv, alice_auth, bob_auth, agent_id = env_alice_bob_agent
    app_id = await _create_app(srv, agent_id, "alice")
    source_id, secret = await _create_source(client, alice_auth)

    r = await client.post(
        "/api/external/subscriptions",
        headers=bob_auth,
        json={
            "source_id": source_id,
            "target_type": "app",
            "target_id": app_id,
            "mode": "app.import",
        },
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "EXTERNAL_SOURCE_NOT_FOUND"

    bob_source_id, _bob_secret = await _create_source(client, bob_auth)
    r = await client.post(
        "/api/external/subscriptions",
        headers=bob_auth,
        json={
            "source_id": bob_source_id,
            "target_type": "app",
            "target_id": app_id,
            "mode": "app.import",
        },
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "EXTERNAL_TARGET_INVALID"

    r = await client.patch(
        f"/api/external/sources/{source_id}",
        headers=alice_auth,
        json={"name": "Renamed", "enabled": False},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Renamed"
    assert r.json()["enabled"] is False

    # Disabled sources reject webhook deliveries.
    r = await _post_webhook(
        client, source_id, secret, external_id="evt-1", payload={"summary": "x"}
    )
    assert r.status_code == 404

    r = await client.post(f"/api/external/sources/{source_id}/rotate-secret", headers=alice_auth)
    assert r.status_code == 200
    new_secret = r.json()["secret"]
    assert new_secret != secret

    r = await client.post(
        "/api/external/subscriptions",
        headers=alice_auth,
        json={
            "source_id": source_id,
            "target_type": "app",
            "target_id": app_id,
            "mode": "app.import",
        },
    )
    assert r.status_code == 200, r.text
    subscription_id = r.json()["subscription_id"]

    r = await client.patch(
        f"/api/external/subscriptions/{subscription_id}",
        headers=alice_auth,
        json={"enabled": False, "coalesce_seconds": 10},
    )
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    assert r.json()["coalesce_seconds"] == 10

    r = await client.delete(f"/api/external/subscriptions/{subscription_id}", headers=alice_auth)
    assert r.status_code == 204

    r = await client.delete(f"/api/external/sources/{source_id}", headers=alice_auth)
    assert r.status_code == 204
    r = await client.get("/api/external/sources", headers=alice_auth)
    assert r.json() == []


async def test_team_judge_produces_autopilot_decision(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, srv, alice_auth, _bob_auth, _agent_id = env_alice_bob_agent
    await _stop_worker(srv)
    assert srv.services is not None
    assert srv.app_runtime is not None
    alice = srv.services.user_repo.get_by_username("alice")
    assert alice is not None
    team_id = "event-team-02"
    AgentRepo(srv.services.db).create(
        agent_id=team_id, user_id=alice.id, name="judging team", kind="team"
    )
    runtime = srv.services.team_runtime_repo.ensure(team_id)
    srv.services.team_runtime_repo.update(
        team_id, expected_version=runtime.version, fields={"mode": "autopilot"}
    )
    coordinator = srv.app_runtime.gateway.team_coordinator
    assert coordinator is not None
    runner = coordinator._decision_runner  # noqa: SLF001 - patch the LLM call
    assert runner is not None

    async def plan_fn(prompt: str, team_id: str) -> tuple[str, dict[str, int]]:
        return (
            json.dumps({"noop": True, "rationale": "external event noted"}),
            {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
        )

    runner._plan_fn = plan_fn  # noqa: SLF001

    source_id, secret = await _create_source(client, alice_auth)
    r = await client.post(
        "/api/external/subscriptions",
        headers=alice_auth,
        json={
            "source_id": source_id,
            "target_type": "team",
            "target_id": team_id,
            "mode": "team.judge",
        },
    )
    assert r.status_code == 200, r.text
    r = await _post_webhook(
        client, source_id, secret, external_id="evt-judge", payload={"summary": "订单 #7"}
    )
    assert r.status_code == 202
    await _drain_worker(srv)

    decisions = []
    for _ in range(100):
        decisions = srv.services.team_decision_repo.list_by_team(team_id, limit=5)
        if decisions:
            break
        await asyncio.sleep(0.05)
    assert decisions, "autopilot decision was not produced"
    assert decisions[0].status == "committed"
    events = srv.services.team_event_repo.list_after(team_id)
    assert any(item.type == "external.event" for item in events)


class FakePollRunner:
    def run(self, *, instance_id: str, tool: str, args: dict[str, object]) -> str:
        return json.dumps(
            {"data": {"items": [{"id": "p1", "title": "轮询事件", "updated_at": 123}]}}
        )

    def list_tool_meta(self, instance_id: str) -> list[dict[str, object]]:
        return [
            {
                "name": "list_orders",
                "description": "orders",
                "read_only": True,
                "input_schema": {},
            }
        ]

    def tool_meta(self, instance_id: str, tool: str) -> dict[str, object] | None:
        return self.list_tool_meta(instance_id)[0] if tool == "list_orders" else None


async def test_connector_poll_source_and_tools(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, srv, alice_auth, _bob_auth, _agent_id = env_alice_bob_agent
    await _stop_worker(srv)
    assert srv.services is not None
    assert srv.app_runtime is not None
    ConnectorRepo(srv.services.db).create(
        instance_id="poll-conn",
        user_id=srv.services.user_repo.get_by_username("alice").id,  # type: ignore[union-attr]
        kind="feishu",
        display_name="Poll connector",
        mcp_server_name="feishu",
    )
    runner = FakePollRunner()
    srv.app_runtime.external_event_service._connector_runner = runner  # noqa: SLF001
    srv.app_runtime.external_event_worker._connector_runner = runner  # noqa: SLF001

    r = await client.get("/api/external/connector-tools?instance_id=poll-conn", headers=alice_auth)
    assert r.status_code == 200, r.text
    assert [item["name"] for item in r.json()] == ["list_orders"]
    assert r.json()[0]["read_only"] is True

    config = {
        "instance_id": "poll-conn",
        "tool": "list_orders",
        "args": {"limit": 10},
        "interval_seconds": 60,
        "event_type": "order.updated",
        "cursor_arg": "since",
        "cursor_path": "updated_at",
        "items_path": "data.items",
        "id_path": "id",
        "summary_path": "title",
    }
    r = await client.post(
        "/api/external/sources",
        headers=alice_auth,
        json={"name": "Poll source", "kind": "connector_poll", "config": config},
    )
    assert r.status_code == 200, r.text
    source = r.json()
    assert source["kind"] == "connector_poll"
    assert source["webhook_path"] == ""
    assert source["config"]["tool"] == "list_orders"

    r = await client.post(f"/api/external/sources/{source['source_id']}/poll", headers=alice_auth)
    assert r.status_code == 200
    worker = srv.app_runtime.external_event_worker
    assert worker is not None
    assert await worker.poll_tick(now=int(time.time())) == 1
    await worker.drain()

    updated = srv.services.external_source_repo.get(source["source_id"])
    assert updated is not None
    assert updated.last_error == ""
    assert updated.cursor_json, updated
    rows = srv.services.external_event_repo.list_for_owner(None)
    assert rows, updated
    events = await client.get(
        f"/api/external/events?source_id={source['source_id']}", headers=alice_auth
    )
    assert [item["summary"] for item in events.json()] == ["轮询事件"]


async def test_expert_notify_target(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, srv, alice_auth, _bob_auth, agent_id = env_alice_bob_agent
    await _stop_worker(srv)
    source_id, secret = await _create_source(client, alice_auth)
    r = await client.post(
        "/api/external/subscriptions",
        headers=alice_auth,
        json={
            "source_id": source_id,
            "target_type": "expert",
            "target_id": agent_id,
            "mode": "expert.notify",
            "template": "[{source}] {summary}",
        },
    )
    assert r.status_code == 200, r.text

    r = await _post_webhook(
        client, source_id, secret, external_id="evt-expert", payload={"summary": "专家通知"}
    )
    assert r.status_code == 202
    await _drain_worker(srv)
    deliveries = await client.get(
        f"/api/external/deliveries?source_id={source_id}", headers=alice_auth
    )
    assert deliveries.json()[0]["status"] == "delivered", deliveries.json()[0]


async def test_team_notify_target(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, srv, alice_auth, _bob_auth, _agent_id = env_alice_bob_agent
    await _stop_worker(srv)
    assert srv.services is not None
    alice = srv.services.user_repo.get_by_username("alice")
    assert alice is not None
    AgentRepo(srv.services.db).create(
        agent_id="event-team-01", user_id=alice.id, name="event team", kind="team"
    )
    source_id, secret = await _create_source(client, alice_auth)
    r = await client.post(
        "/api/external/subscriptions",
        headers=alice_auth,
        json={
            "source_id": source_id,
            "target_type": "team",
            "target_id": "event-team-01",
            "mode": "team.notify",
        },
    )
    assert r.status_code == 200, r.text

    r = await _post_webhook(
        client, source_id, secret, external_id="evt-team", payload={"summary": "团队通知"}
    )
    assert r.status_code == 202
    await _drain_worker(srv)
    deliveries = await client.get(
        f"/api/external/deliveries?source_id={source_id}", headers=alice_auth
    )
    assert deliveries.json()[0]["status"] == "delivered"
