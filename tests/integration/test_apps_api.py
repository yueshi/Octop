"""Integration tests for the light-app HTTP API and sharing ACL."""

from __future__ import annotations

import json

import httpx
import pytest
from langchain_core.messages import AIMessage

from octop.infra.apps.service import AppService
from octop.infra.server import OctopServer

COLUMNS: list[dict[str, object]] = [
    {"name": "title", "type": "string", "required": True},
    {"name": "status", "type": "enum", "values": ["new", "done"]},
]


def _service(srv: OctopServer) -> AppService:
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
    row = await _service(srv).create(
        agent_id=agent_id,
        owner_user_id=owner.id,
        name="Tracker",
        description="things",
        columns=COLUMNS,
    )
    return row.app_id


async def test_owner_app_flow(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, srv, alice_auth, _bob_auth, agent_id = env_alice_bob_agent
    app_id = await _create_app(srv, agent_id, "alice")

    r = await client.get("/api/apps", headers=alice_auth)
    assert r.status_code == 200
    apps = r.json()["apps"]
    assert [item["app_id"] for item in apps] == [app_id]
    assert apps[0]["permission"] == "edit_structure"
    assert apps[0]["owner_username"] == "alice"
    assert apps[0]["has_html"] is False
    assert apps[0]["html_bytes"] == 0
    assert apps[0]["data_bytes"] > 0

    r = await client.get(f"/api/apps/{app_id}/files", headers=alice_auth)
    assert r.status_code == 200
    files = {item["name"]: item for item in r.json()["files"]}
    assert set(files) == {"app.html", "data.csv", "schema.json", "manifest.json"}
    assert files["data.csv"]["size"] > 0
    assert files["data.csv"]["path"] == f"apps/{app_id}/data.csv"

    r = await client.get(f"/api/apps/{app_id}", headers=alice_auth)
    assert r.status_code == 200
    detail = r.json()
    assert detail["name"] == "Tracker"
    assert detail["has_html"] is False
    assert [col["name"] for col in detail["columns"]] == ["title", "status"]
    assert [item["table_key"] for item in detail["tables"]] == ["main"]
    assert detail["tables"][0]["is_main"] is True
    assert [col["name"] for col in detail["tables"][0]["columns"]] == ["title", "status"]

    r = await client.get(f"/api/apps/{app_id}/tables", headers=alice_auth)
    assert r.status_code == 200
    assert [item["table_key"] for item in r.json()["tables"]] == ["main"]

    r = await client.get(f"/api/apps/{app_id}/html", headers=alice_auth)
    assert r.status_code == 200
    assert r.text == ""

    await _service(srv).write_html(_service(srv).get(app_id), "<h1>Hi</h1>")
    r = await client.get("/api/apps", headers=alice_auth)
    assert r.json()["apps"][0]["has_html"] is True
    assert r.json()["apps"][0]["html_bytes"] == len("<h1>Hi</h1>")

    r = await client.get(f"/api/apps/{app_id}/html", headers=alice_auth)
    assert r.status_code == 200
    assert r.text == "<h1>Hi</h1>"
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["content-security-policy"] == "sandbox"
    assert r.headers["x-content-type-options"] == "nosniff"

    r = await client.post(
        f"/api/apps/{app_id}/rows",
        headers=alice_auth,
        json={"row": {"title": "A", "status": "new"}},
    )
    assert r.status_code == 200
    appended = r.json()
    assert appended["row_index"] == 0
    assert appended["row"] == {"title": "A", "status": "new"}
    assert appended["table"] == "main"
    assert appended["row_id"].startswith("row_")
    assert appended["version"] == 1

    r = await client.get(f"/api/apps/{app_id}/rows?limit=10", headers=alice_auth)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["table"] == "main"
    assert body["rows"] == [{"title": "A", "status": "new"}]

    r = await client.post(
        f"/api/apps/{app_id}/rows",
        headers=alice_auth,
        json={"row": {"title": "B", "nope": "1"}},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "APP_ROW_INVALID"

    r = await client.patch(
        f"/api/apps/{app_id}",
        headers=alice_auth,
        json={"name": "Renamed", "description": ""},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Renamed"
    assert r.json()["description"] == ""

    r = await client.delete(f"/api/apps/{app_id}", headers=alice_auth)
    assert r.status_code == 200
    assert r.json() == {"deleted": app_id}
    r = await client.get(f"/api/apps/{app_id}", headers=alice_auth)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "APP_NOT_FOUND"


async def test_sharing_grants_view_then_edit_data(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, srv, alice_auth, bob_auth, agent_id = env_alice_bob_agent
    app_id = await _create_app(srv, agent_id, "alice")

    r = await client.get("/api/apps", headers=bob_auth)
    assert r.json()["apps"] == []
    for r in (
        await client.get(f"/api/apps/{app_id}", headers=bob_auth),
        await client.get(f"/api/apps/{app_id}/tables", headers=bob_auth),
        await client.get(f"/api/apps/{app_id}/rows", headers=bob_auth),
        await client.get(f"/api/apps/{app_id}/shares", headers=bob_auth),
        await client.post(
            f"/api/apps/{app_id}/rows",
            headers=bob_auth,
            json={"row": {"title": "B"}},
        ),
    ):
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "APP_FORBIDDEN"

    r = await client.put(
        f"/api/apps/{app_id}/shares",
        headers=alice_auth,
        json={"shares": [{"username": "bob", "permission": "view"}]},
    )
    assert r.status_code == 200
    shares = r.json()["shares"]
    assert [(s["username"], s["permission"]) for s in shares] == [("bob", "view")]

    r = await client.get("/api/apps", headers=bob_auth)
    assert [(item["app_id"], item["permission"]) for item in r.json()["apps"]] == [(app_id, "view")]
    r = await client.get(f"/api/apps/{app_id}", headers=bob_auth)
    assert r.status_code == 200
    r = await client.get(f"/api/apps/{app_id}/rows", headers=bob_auth)
    assert r.status_code == 200
    r = await client.post(
        f"/api/apps/{app_id}/rows",
        headers=bob_auth,
        json={"row": {"title": "B"}},
    )
    assert r.status_code == 403
    r = await client.patch(f"/api/apps/{app_id}", headers=bob_auth, json={"name": "B"})
    assert r.status_code == 403

    r = await client.put(
        f"/api/apps/{app_id}/shares",
        headers=alice_auth,
        json={"shares": [{"username": "bob", "permission": "edit_data"}]},
    )
    assert r.status_code == 200
    r = await client.post(
        f"/api/apps/{app_id}/rows",
        headers=bob_auth,
        json={"row": {"title": "B"}},
    )
    assert r.status_code == 200
    assert r.json()["row_index"] == 0
    r = await client.get(f"/api/apps/{app_id}/rows", headers=bob_auth)
    assert r.json()["total"] == 1

    r = await client.put(
        f"/api/apps/{app_id}/shares",
        headers=alice_auth,
        json={"shares": []},
    )
    assert r.json()["shares"] == []
    r = await client.get(f"/api/apps/{app_id}", headers=bob_auth)
    assert r.status_code == 403


async def test_multi_table_rows_endpoints(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, srv, alice_auth, _bob_auth, agent_id = env_alice_bob_agent
    app_id = await _create_app(srv, agent_id, "alice")
    service = _service(srv)
    await service.create_table(
        service.get(app_id),
        table_key="orders",
        label="Orders",
        columns=[{"name": "item", "type": "string", "required": True}],
    )

    r = await client.get(f"/api/apps/{app_id}/tables", headers=alice_auth)
    assert r.status_code == 200
    tables = r.json()["tables"]
    assert [item["table_key"] for item in tables] == ["main", "orders"]
    assert tables[1]["label"] == "Orders"
    assert tables[1]["is_main"] is False
    assert [col["name"] for col in tables[1]["columns"]] == ["item"]

    r = await client.get(f"/api/apps/{app_id}", headers=alice_auth)
    assert [item["table_key"] for item in r.json()["tables"]] == ["main", "orders"]

    r = await client.post(
        f"/api/apps/{app_id}/rows",
        headers=alice_auth,
        json={"row": {"item": "cup"}, "table": "orders"},
    )
    assert r.status_code == 200
    assert r.json()["table"] == "orders"

    r = await client.get(f"/api/apps/{app_id}/rows?table=orders", headers=alice_auth)
    body = r.json()
    assert body["table"] == "orders"
    assert body["total"] == 1
    assert body["rows"] == [{"item": "cup"}]

    r = await client.get(f"/api/apps/{app_id}/rows", headers=alice_auth)
    assert r.json()["total"] == 0

    # The card counter aggregates every table.
    r = await client.get("/api/apps", headers=alice_auth)
    assert r.json()["apps"][0]["row_count"] == 1

    r = await client.get(f"/api/apps/{app_id}/rows?table=nope", headers=alice_auth)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "APP_SCHEMA_INVALID"


async def test_share_validation(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, srv, alice_auth, _bob_auth, agent_id = env_alice_bob_agent
    app_id = await _create_app(srv, agent_id, "alice")

    for shares in (
        [{"username": "nobody", "permission": "view"}],
        [{"username": "bob", "permission": "view"}, {"username": "bob", "permission": "view"}],
        [{"username": "alice", "permission": "view"}],
    ):
        r = await client.put(
            f"/api/apps/{app_id}/shares",
            headers=alice_auth,
            json={"shares": shares},
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "APP_SHARE_INVALID"


async def test_unknown_app_returns_404(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, _srv, alice_auth, _bob_auth, _agent_id = env_alice_bob_agent
    for app_id in ("app_nope", "not-an-app"):
        r = await client.get(f"/api/apps/{app_id}", headers=alice_auth)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "APP_NOT_FOUND"


async def test_assistant_history_acl_and_guards(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, srv, alice_auth, bob_auth, agent_id = env_alice_bob_agent
    app_id = await _create_app(srv, agent_id, "alice")

    # History starts empty and is per-user, oldest first.
    r = await client.get(f"/api/apps/{app_id}/assistant/history", headers=alice_auth)
    assert r.status_code == 200
    assert r.json() == {"messages": []}

    # Non-viewers cannot reach the assistant endpoints.
    r = await client.get(f"/api/apps/{app_id}/assistant/history", headers=bob_auth)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "APP_FORBIDDEN"
    r = await client.post(
        f"/api/apps/{app_id}/assistant", headers=bob_auth, json={"question": "hi"}
    )
    assert r.status_code == 403

    # Empty (whitespace-only) questions are rejected with 400.
    r = await client.post(
        f"/api/apps/{app_id}/assistant", headers=alice_auth, json={"question": "   "}
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "APP_QUESTION_INVALID"

    # Asking streams the scripted answer as SSE frames and records history.
    assert srv.services is not None
    alice = srv.services.user_repo.get_by_username("alice")
    assert alice is not None
    agent = srv.app_runtime.agent_registry.get_agent(agent_id)
    agent._assistant_llm.script = [
        AIMessage(
            content="There are 3 rows.",
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )
    ]
    r = await client.post(
        f"/api/apps/{app_id}/assistant", headers=alice_auth, json={"question": "how many rows?"}
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = [
        json.loads(block.split("data: ", 1)[1]) for block in r.text.strip().split("\n\n") if block
    ]
    assert [event["type"] for event in events] == ["token", "done"]
    assert "".join(event.get("text", "") for event in events) == "There are 3 rows."
    assert events[-1]["input_tokens"] == 10

    r = await client.get(f"/api/apps/{app_id}/assistant/history", headers=alice_auth)
    assert r.status_code == 200
    messages = r.json()["messages"]
    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "how many rows?"),
        ("assistant", "There are 3 rows."),
    ]

    # Seeded history is returned oldest-first for its owner only.
    repo = srv.services.light_app_repo
    repo.add_assistant_message(app_id, alice.id, role="user", content="how many?")
    repo.add_assistant_message(app_id, alice.id, role="assistant", content="3 rows")
    r = await client.get(f"/api/apps/{app_id}/assistant/history", headers=alice_auth)
    assert r.status_code == 200
    messages = r.json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    assert [m["content"] for m in messages] == [
        "how many rows?",
        "There are 3 rows.",
        "how many?",
        "3 rows",
    ]
    assert all(m["created_at"] > 0 for m in messages)


async def _upload_import_file(
    client: httpx.AsyncClient,
    auth: dict[str, str],
    app_id: str,
    content: bytes = b"title,qty,status\ncup,3,new\npen,1,done\n",
    filename: str = "items.csv",
) -> str:
    r = await client.post(
        f"/api/apps/{app_id}/upload",
        headers=auth,
        files={"file": (filename, content, "text/csv")},
    )
    assert r.status_code == 200, r.text
    return r.json()["path"]


async def test_upload_and_import_flow(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, srv, alice_auth, _bob_auth, agent_id = env_alice_bob_agent
    app_id = await _create_app(srv, agent_id, "alice")

    path = await _upload_import_file(client, alice_auth, app_id)
    assert path.startswith("inbound/imports/")

    r = await client.post(
        f"/api/apps/{app_id}/tables/main/import",
        headers=alice_auth,
        json={"kind": "upload", "path": path, "mode": "append"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["table"], body["imported"], body["skipped"]) == ("main", 2, 0)
    assert body["errors"] == []

    # Appending the same file again duplicates rows (no dedup for manual imports).
    r = await client.post(
        f"/api/apps/{app_id}/tables/main/import",
        headers=alice_auth,
        json={"kind": "upload", "path": path, "mode": "append"},
    )
    assert r.json()["imported"] == 2

    r = await client.get(f"/api/apps/{app_id}/rows", headers=alice_auth)
    assert r.json()["total"] == 4

    # Replace wipes the previous rows and honors validation skips.
    path2 = await _upload_import_file(
        client,
        alice_auth,
        app_id,
        content=b"title,status\nbox,done\nbad,pending\n",
    )
    r = await client.post(
        f"/api/apps/{app_id}/tables/main/import",
        headers=alice_auth,
        json={"kind": "upload", "path": path2, "mode": "replace"},
    )
    assert r.status_code == 200
    body = r.json()
    assert (body["imported"], body["skipped"]) == (1, 1)
    r = await client.get(f"/api/apps/{app_id}/rows", headers=alice_auth)
    assert r.json()["total"] == 1

    # Guards: unknown table, hidden path, unsupported mode payload.
    r = await client.post(
        f"/api/apps/{app_id}/tables/nope/import",
        headers=alice_auth,
        json={"kind": "upload", "path": path},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "APP_SCHEMA_INVALID"
    r = await client.post(
        f"/api/apps/{app_id}/tables/main/import",
        headers=alice_auth,
        json={"kind": "workspace_file", "path": ".octop/auth/tokens.json"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "APP_ROW_INVALID"


async def test_import_permissions(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, srv, alice_auth, bob_auth, agent_id = env_alice_bob_agent
    app_id = await _create_app(srv, agent_id, "alice")

    path = await _upload_import_file(client, alice_auth, app_id)
    import_url = f"/api/apps/{app_id}/tables/main/import"

    # No share: everything 403.
    r = await client.post(import_url, headers=bob_auth, json={"kind": "upload", "path": path})
    assert r.status_code == 403

    # view: cannot upload or import.
    r = await client.put(
        f"/api/apps/{app_id}/shares",
        headers=alice_auth,
        json={"shares": [{"username": "bob", "permission": "view"}]},
    )
    assert r.status_code == 200
    r = await client.post(
        f"/api/apps/{app_id}/upload",
        headers=bob_auth,
        files={"file": ("a.csv", b"title\nx\n", "text/csv")},
    )
    assert r.status_code == 403
    r = await client.post(import_url, headers=bob_auth, json={"kind": "upload", "path": path})
    assert r.status_code == 403

    # edit_data: can upload and append-import, but not replace or workspace_file.
    r = await client.put(
        f"/api/apps/{app_id}/shares",
        headers=alice_auth,
        json={"shares": [{"username": "bob", "permission": "edit_data"}]},
    )
    assert r.status_code == 200
    bob_path = await _upload_import_file(client, bob_auth, app_id)
    r = await client.post(import_url, headers=bob_auth, json={"kind": "upload", "path": bob_path})
    assert r.status_code == 200
    r = await client.post(
        import_url,
        headers=bob_auth,
        json={"kind": "upload", "path": bob_path, "mode": "replace"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "APP_FORBIDDEN"
    r = await client.post(
        import_url,
        headers=bob_auth,
        json={"kind": "workspace_file", "path": "inbound/imports/x.csv"},
    )
    assert r.status_code == 403


async def test_sources_lifecycle_and_permissions(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from octop.infra.connectors.service import ConnectorService
    from octop.infra.db.repos.secrets import SecretRepo
    from octop.infra.db.repos.settings import SettingsRepo

    client, srv, alice_auth, bob_auth, agent_id = env_alice_bob_agent
    app_id = await _create_app(srv, agent_id, "alice")
    assert srv.services is not None

    # A gateway connector instance owned by alice (run uses its stored creds).
    repo = srv.services.repos.connector_repo
    repo.create(
        instance_id="conn1",
        user_id=srv.services.user_repo.get_by_username("alice").id,  # type: ignore[union-attr]
        kind="baidu-map",
        display_name="Map",
        mcp_server_name="baidu_map__conn1",
    )
    ConnectorService(
        repo=repo,
        secret_repo=SecretRepo(srv.services.db),
        settings_repo=SettingsRepo(srv.services.db),
        config=srv.services.config,
    ).encrypt_and_store(instance_id="conn1", payload={"api_key": "map-key"})
    monkeypatch.setattr(
        "octop.infra.apps.sources.mcp_tools_for_kind",
        lambda kind: [
            {"name": "map_search", "description": "Search", "inputSchema": {"type": "object"}},
        ],
    )

    # Empty list initially.
    r = await client.get(f"/api/apps/{app_id}/sources", headers=alice_auth)
    assert r.status_code == 200
    assert r.json()["sources"] == []

    # Tool listing for the binding dialog.
    r = await client.get(
        f"/api/apps/{app_id}/source-tools", headers=alice_auth, params={"instance_id": "conn1"}
    )
    assert r.status_code == 200, r.text
    tools = r.json()["tools"]
    assert [t["name"] for t in tools] == ["map_search"]
    assert tools[0]["read_only"] is True
    assert tools[0]["input_schema"] == {"type": "object"}

    # Bind an external source to main.
    r = await client.put(
        f"/api/apps/{app_id}/sources/src_1",
        headers=alice_auth,
        json={
            "table_key": "main",
            "kind": "external",
            "mode": "append",
            "params": {
                "instance_id": "conn1",
                "tool": "map_search",
                "args": {"q": "lake"},
                "result_path": "",
            },
            "mapping": {"name": "title"},
        },
    )
    assert r.status_code == 200, r.text
    view = r.json()
    assert view["table_key"] == "main" and view["kind"] == "external"
    assert view["last_status"] is None

    # Second binding on the same table clashes.
    r = await client.put(
        f"/api/apps/{app_id}/sources/src_2",
        headers=alice_auth,
        json={
            "table_key": "main",
            "kind": "external",
            "params": {"instance_id": "conn1", "tool": "map_search"},
        },
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "APP_SOURCE_INVALID"

    # Unknown connector / unknown tool rejected at bind time.
    r = await client.put(
        f"/api/apps/{app_id}/sources/src_3",
        headers=alice_auth,
        json={
            "table_key": "main",
            "kind": "external",
            "params": {"instance_id": "missing", "tool": "map_search"},
        },
    )
    assert r.status_code == 400
    r = await client.put(
        f"/api/apps/{app_id}/sources/src_3",
        headers=alice_auth,
        json={
            "table_key": "main",
            "kind": "external",
            "params": {"instance_id": "conn1", "tool": "no_such"},
        },
    )
    assert r.status_code == 400

    # Run the external source: the fake gateway returns one matching row.
    def fake_call(kind: str, creds: dict, name: str, args: dict) -> str:
        return '[{"name": "West Lake"}]'

    monkeypatch.setattr("octop.infra.apps.sources.call_gateway_tool", fake_call)
    r = await client.post(f"/api/apps/{app_id}/sources/src_1/run", headers=alice_auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["table"], body["imported"], body["skipped"]) == ("main", 1, 0)

    r = await client.get(f"/api/apps/{app_id}/sources", headers=alice_auth)
    view = r.json()["sources"][0]
    assert view["last_status"] == "ok" and view["last_row_count"] == 1

    # ACL: bob without a share cannot see or touch sources.
    r = await client.get(f"/api/apps/{app_id}/sources", headers=bob_auth)
    assert r.status_code == 403
    r = await client.put(
        f"/api/apps/{app_id}/sources/src_9",
        headers=bob_auth,
        json={
            "table_key": "main",
            "kind": "workspace_file",
            "params": {"path": "a.csv"},
        },
    )
    assert r.status_code == 403

    # view share: can list, cannot bind or run.
    r = await client.put(
        f"/api/apps/{app_id}/shares",
        headers=alice_auth,
        json={"shares": [{"username": "bob", "permission": "view"}]},
    )
    assert r.status_code == 200
    r = await client.get(f"/api/apps/{app_id}/sources", headers=bob_auth)
    assert r.status_code == 200
    r = await client.post(f"/api/apps/{app_id}/sources/src_1/run", headers=bob_auth)
    assert r.status_code == 403

    # edit_data share: can run, still cannot bind (needs edit_structure).
    r = await client.put(
        f"/api/apps/{app_id}/shares",
        headers=alice_auth,
        json={"shares": [{"username": "bob", "permission": "edit_data"}]},
    )
    assert r.status_code == 200
    r = await client.post(f"/api/apps/{app_id}/sources/src_1/run", headers=bob_auth)
    assert r.status_code == 200
    r = await client.put(
        f"/api/apps/{app_id}/sources/src_9",
        headers=bob_auth,
        json={
            "table_key": "main",
            "kind": "workspace_file",
            "params": {"path": "a.csv"},
        },
    )
    assert r.status_code == 403

    # Delete the binding; runs then 404.
    r = await client.delete(f"/api/apps/{app_id}/sources/src_1", headers=alice_auth)
    assert r.status_code == 204
    r = await client.post(f"/api/apps/{app_id}/sources/src_1/run", headers=alice_auth)
    assert r.status_code == 404


async def test_import_dry_run_and_header_row(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, srv, alice_auth, _bob_auth, agent_id = env_alice_bob_agent
    app_id = await _create_app(srv, agent_id, "alice")

    path = await _upload_import_file(
        client,
        alice_auth,
        app_id,
        content="2026 年 9 月工资表\ntitle,qty,status\ncup,3,new\n".encode(),
    )

    # A dry run counts the rows without writing them. The header hint points
    # at the real header line; without it the title line is taken as the
    # header and every row is rejected.
    r = await client.post(
        f"/api/apps/{app_id}/tables/main/import",
        headers=alice_auth,
        json={
            "kind": "upload",
            "path": path,
            "mode": "append",
            "header_row": 1,
            "dry_run": True,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert body["would_import"] == 1
    assert body["imported"] == 0

    r = await client.get(f"/api/apps/{app_id}/rows", headers=alice_auth)
    assert r.json()["total"] == 0

    r = await client.post(
        f"/api/apps/{app_id}/tables/main/import",
        headers=alice_auth,
        json={
            "kind": "upload",
            "path": path,
            "mode": "append",
            "header_row": 1,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 1

    r = await client.get(f"/api/apps/{app_id}/rows", headers=alice_auth)
    assert r.json()["rows"][0]["title"] == "cup"


async def test_transfer_moves_the_app_between_agents(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, srv, alice_auth, _bob_auth, agent_id = env_alice_bob_agent
    app_id = await _create_app(srv, agent_id, "alice")

    r = await client.post(
        "/api/agents",
        headers=alice_auth,
        json={"name": "second-bot", "config": {"providers": ["openai"]}},
    )
    assert r.status_code == 201, r.text
    other_agent_id = r.json()["agent_id"]

    r = await client.post(
        f"/api/apps/{app_id}/transfer",
        headers=alice_auth,
        json={"agent_id": other_agent_id},
    )
    assert r.status_code == 200, r.text
    assert r.json()["agent_id"] == other_agent_id

    r = await client.get(f"/api/apps/{app_id}", headers=alice_auth)
    assert r.status_code == 200
    assert r.json()["agent_id"] == other_agent_id


async def test_agent_delete_is_refused_while_it_owns_apps(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, srv, alice_auth, _bob_auth, agent_id = env_alice_bob_agent
    await _create_app(srv, agent_id, "alice")

    r = await client.delete(f"/api/agents/{agent_id}", headers=alice_auth)
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "AGENT_HAS_APPS"

    # The agent is still there.
    r = await client.get(f"/api/agents/{agent_id}", headers=alice_auth)
    assert r.status_code == 200


async def test_asset_upload_and_fetch(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, srv, alice_auth, bob_auth, agent_id = env_alice_bob_agent
    app_id = await _create_app(srv, agent_id, "alice")
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 16

    r = await client.post(
        f"/api/apps/{app_id}/assets",
        headers=alice_auth,
        files={"file": ("cover.png", png, "image/png")},
    )
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "cover.png"

    r = await client.get(f"/api/apps/{app_id}/assets", headers=alice_auth)
    assert [item["name"] for item in r.json()["assets"]] == ["cover.png"]

    r = await client.get(f"/api/apps/{app_id}/assets/cover.png", headers=alice_auth)
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == png

    # A non-collaborator cannot fetch the asset.
    r = await client.get(f"/api/apps/{app_id}/assets/cover.png", headers=bob_auth)
    assert r.status_code == 403


async def test_asset_upload_rejects_non_images(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, srv, alice_auth, _bob_auth, agent_id = env_alice_bob_agent
    app_id = await _create_app(srv, agent_id, "alice")

    r = await client.post(
        f"/api/apps/{app_id}/assets",
        headers=alice_auth,
        files={"file": ("cover.svg", b"<svg></svg>", "image/svg+xml")},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "APP_SOURCE_INVALID"


async def test_row_edit_export_and_idempotent_append(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, srv, alice_auth, _bob_auth, agent_id = env_alice_bob_agent
    app_id = await _create_app(srv, agent_id, "alice")

    r = await client.post(
        f"/api/apps/{app_id}/rows",
        headers=alice_auth,
        json={"row": {"title": "A", "status": "new"}, "client_request_id": "req-1"},
    )
    assert r.status_code == 200, r.text
    created = r.json()
    assert created["row_id"]
    assert created["version"] == 1

    # Retrying the same id is a no-op.
    r = await client.post(
        f"/api/apps/{app_id}/rows",
        headers=alice_auth,
        json={"row": {"title": "A", "status": "new"}, "client_request_id": "req-1"},
    )
    assert r.json()["row_id"] == created["row_id"]
    r = await client.get(f"/api/apps/{app_id}/rows", headers=alice_auth)
    assert r.json()["total"] == 1

    meta = r.json()["row_meta"][0]
    assert meta["row_id"] == created["row_id"]
    assert meta["version"] == 1

    r = await client.patch(
        f"/api/apps/{app_id}/rows/{created['row_id']}",
        headers=alice_auth,
        json={"row": {"title": "B", "status": "done"}, "expected_version": 1},
    )
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2
    assert r.json()["row"]["title"] == "B"

    # A stale version is a conflict, not a silent overwrite.
    r = await client.patch(
        f"/api/apps/{app_id}/rows/{created['row_id']}",
        headers=alice_auth,
        json={"row": {"title": "C", "status": "new"}, "expected_version": 1},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "APP_CONFLICT"

    r = await client.get(f"/api/apps/{app_id}/export", headers=alice_auth)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert r.text == "title,status\nB,done\n"

    r = await client.delete(
        f"/api/apps/{app_id}/rows/{created['row_id']}?expected_version=2",
        headers=alice_auth,
    )
    assert r.status_code == 200
    assert r.json()["deleted"] == created["row_id"]
    r = await client.get(f"/api/apps/{app_id}/rows", headers=alice_auth)
    assert r.json()["total"] == 0


async def test_alter_schema_endpoint(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, srv, alice_auth, _bob_auth, agent_id = env_alice_bob_agent
    app_id = await _create_app(srv, agent_id, "alice")
    await client.post(
        f"/api/apps/{app_id}/rows",
        headers=alice_auth,
        json={"row": {"title": "A", "status": "new"}},
    )

    r = await client.patch(
        f"/api/apps/{app_id}/schema",
        headers=alice_auth,
        json={"renames": {"title": "name"}},
    )
    assert r.status_code == 200, r.text
    assert [column["name"] for column in r.json()["columns"]] == ["name", "status"]

    r = await client.get(f"/api/apps/{app_id}/rows", headers=alice_auth)
    assert r.json()["rows"] == [{"name": "A", "status": "new"}]

    # Dropping a column removes its data.
    r = await client.patch(
        f"/api/apps/{app_id}/schema",
        headers=alice_auth,
        json={"drops": ["status"]},
    )
    assert r.status_code == 200, r.text
    assert [column["name"] for column in r.json()["columns"]] == ["name"]


async def test_import_upsert_key_over_http(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, srv, alice_auth, _bob_auth, agent_id = env_alice_bob_agent
    app_id = await _create_app(srv, agent_id, "alice")
    path = await _upload_import_file(client, alice_auth, app_id)

    r = await client.post(
        f"/api/apps/{app_id}/tables/main/import",
        headers=alice_auth,
        json={"kind": "upload", "path": path, "upsert_key": ["title"]},
    )
    assert r.status_code == 200, r.text
    assert (r.json()["imported"], r.json()["updated"]) == (2, 0)

    r = await client.post(
        f"/api/apps/{app_id}/tables/main/import",
        headers=alice_auth,
        json={"kind": "upload", "path": path, "upsert_key": ["title"]},
    )
    assert r.status_code == 200, r.text
    assert (r.json()["imported"], r.json()["updated"]) == (0, 2)

    r = await client.get(f"/api/apps/{app_id}/rows", headers=alice_auth)
    assert r.json()["total"] == 2


async def test_import_multiple_workspace_files(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, srv, alice_auth, _bob_auth, agent_id = env_alice_bob_agent
    app_id = await _create_app(srv, agent_id, "alice")
    first = await _upload_import_file(
        client,
        alice_auth,
        app_id,
        content=b"title,status\nA,new\n",
        filename="sep.csv",
    )
    second = await _upload_import_file(
        client,
        alice_auth,
        app_id,
        content=b"title,status\nB,done\n",
        filename="oct.csv",
    )

    r = await client.post(
        f"/api/apps/{app_id}/tables/main/import",
        headers=alice_auth,
        json={"kind": "workspace_file", "paths": [first, second]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 2


async def test_create_app_from_expert_template(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, srv, alice_auth, _bob_auth, agent_id = env_alice_bob_agent
    assert srv.app_runtime is not None
    workspace = srv.app_runtime.agent_registry.workspace_for_agent(agent_id)
    assert workspace is not None
    await workspace.awrite_text(
        "app_templates/reading/schema.json",
        json.dumps(
            {
                "version": 1,
                "columns": [{"name": "title", "label": "书名", "type": "text", "required": True}],
            }
        ),
    )
    await workspace.awrite_text(
        "app_templates/reading/app.html",
        "<!doctype html><html><body><h1>Books</h1></body></html>",
    )

    r = await client.get(f"/api/apps/templates?agent_id={agent_id}", headers=alice_auth)
    assert r.status_code == 200, r.text
    assert r.json()["templates"][0]["template"] == "reading"

    r = await client.post(
        "/api/apps/from-template",
        headers=alice_auth,
        json={"agent_id": agent_id, "template": "reading", "name": "My reading"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "My reading"
    assert body["row_count"] == 0
    assert body["has_html"] is True
    assert [column["name"] for column in body["columns"]] == ["title"]


async def test_public_link_is_read_only_and_revocable(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, srv, alice_auth, _bob_auth, agent_id = env_alice_bob_agent
    app_id = await _create_app(srv, agent_id, "alice")
    service = _service(srv)
    row = service.get(app_id)
    await service.append_row(row, {"title": "A", "status": "new"})
    await service.write_html(row, "<!doctype html><html><body><h1>Public</h1></body></html>")

    r = await client.post(
        f"/api/apps/{app_id}/publication",
        headers=alice_auth,
        json={"ttl_days": 0},
    )
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    assert token
    assert r.json()["url"] == f"/p/{token}"

    # No JWT on the public endpoints.
    r = await client.get(f"/api/public/apps/{token}")
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Tracker"
    assert "Public" in r.json()["html"]

    r = await client.get(f"/api/public/apps/{token}/rows")
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == [{"title": "A", "status": "new"}]

    r = await client.get(f"/p/{token}")
    assert r.status_code == 200
    assert "octopApp" in r.text

    # Revoking takes effect immediately.
    r = await client.delete(f"/api/apps/{app_id}/publication", headers=alice_auth)
    assert r.status_code == 200
    r = await client.get(f"/api/public/apps/{token}")
    assert r.status_code == 404


async def test_backup_download_and_restore_api(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, srv, alice_auth, _bob_auth, agent_id = env_alice_bob_agent
    app_id = await _create_app(srv, agent_id, "alice")
    service = _service(srv)
    row = service.get(app_id)
    await service.append_row(row, {"title": "A", "status": "new"})
    await service.write_html(row, "<!doctype html><html><body><h1>Backup</h1></body></html>")

    r = await client.get(f"/api/apps/{app_id}/backup", headers=alice_auth)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    assert r.content.startswith(b"PK")

    r = await client.post(
        "/api/apps/restore",
        params={"agent_id": agent_id},
        headers=alice_auth,
        files={"file": ("backup.zip", r.content, "application/zip")},
    )
    assert r.status_code == 201, r.text
    restored = r.json()
    assert restored["app_id"] != app_id
    assert restored["agent_id"] == agent_id
    assert restored["name"] == "Tracker"
    assert restored["row_count"] == 1
    assert restored["has_html"] is True

    r = await client.get(f"/api/apps/{restored['app_id']}/rows", headers=alice_auth)
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == [{"title": "A", "status": "new"}]

    r = await client.post(
        "/api/apps/restore",
        params={"agent_id": agent_id},
        headers=alice_auth,
        files={"file": ("backup.zip", b"not a zip", "application/zip")},
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "APP_SOURCE_INVALID"


async def test_pii_is_masked_for_viewers_and_public_links(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, srv, alice_auth, bob_auth, agent_id = env_alice_bob_agent
    assert srv.services is not None
    owner = srv.services.user_repo.get_by_username("alice")
    assert owner is not None
    service = _service(srv)
    row = await service.create(
        agent_id=agent_id,
        owner_user_id=owner.id,
        name="Contacts",
        columns=[
            {"name": "title", "type": "string"},
            {"name": "phone", "type": "string", "pii": "phone"},
        ],
    )
    await service.append_row(row, {"title": "A", "phone": "13800138000"})
    app_id = row.app_id

    r = await client.put(
        f"/api/apps/{app_id}/shares",
        headers=alice_auth,
        json={"shares": [{"username": "bob", "permission": "view"}]},
    )
    assert r.status_code == 200, r.text

    r = await client.get(f"/api/apps/{app_id}/rows", headers=bob_auth)
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == [{"title": "A", "phone": "••••"}]
    assert r.json()["pii_columns"] == ["phone"]

    r = await client.get(f"/api/apps/{app_id}/export", headers=bob_auth)
    assert r.status_code == 200, r.text
    assert "13800138000" not in r.text
    assert "••••" in r.text

    # Editors still see the real value.
    r = await client.put(
        f"/api/apps/{app_id}/shares",
        headers=alice_auth,
        json={"shares": [{"username": "bob", "permission": "edit_data"}]},
    )
    assert r.status_code == 200
    r = await client.get(f"/api/apps/{app_id}/rows", headers=bob_auth)
    assert r.json()["rows"] == [{"title": "A", "phone": "13800138000"}]
    assert r.json()["pii_columns"] == []

    # Only structure editors may download an unmasked backup.
    r = await client.get(f"/api/apps/{app_id}/backup", headers=bob_auth)
    assert r.status_code == 403
    r = await client.get(f"/api/apps/{app_id}/backup", headers=alice_auth)
    assert r.status_code == 200
    assert r.content.startswith(b"PK")

    # Public links are always read-only and masked.
    r = await client.post(
        f"/api/apps/{app_id}/publication",
        headers=alice_auth,
        json={"ttl_days": 0},
    )
    token = r.json()["token"]
    r = await client.get(f"/api/public/apps/{token}/rows")
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == [{"title": "A", "phone": "••••"}]
    assert "13800138000" not in r.text
