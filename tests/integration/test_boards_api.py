"""Integration tests for the workbench board HTTP API."""

from __future__ import annotations

import httpx

from octop.infra.server import OctopServer


def _item(app_id: str, *, x: int = 0, y: int = 0, w: int = 4, h: int = 4) -> dict[str, object]:
    return {"app_id": app_id, "x": x, "y": y, "w": w, "h": h}


def _app_id(srv: OctopServer, username: str, agent_id: str, name: str) -> str:
    assert srv.services is not None
    owner = srv.services.user_repo.get_by_username(username)
    assert owner is not None
    row = srv.services.light_app_repo.create(
        agent_id=agent_id,
        owner_user_id=owner.id,
        name=name,
    )
    return row.app_id


async def test_board_crud_and_layout_flow(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, srv, alice_auth, _bob_auth, agent_id = env_alice_bob_agent
    app_id = _app_id(srv, "alice", agent_id, "Tracker")

    r = await client.post(
        "/api/boards",
        headers=alice_auth,
        json={"name": "Home", "description": "daily apps"},
    )
    assert r.status_code == 201
    board = r.json()
    board_id = board["board_id"]
    assert board["name"] == "Home"
    assert board["version"] == 1
    assert board["item_count"] == 0
    assert board["layout"] == {"version": 1, "items": []}

    r = await client.get("/api/boards", headers=alice_auth)
    assert r.status_code == 200
    assert [item["board_id"] for item in r.json()] == [board_id]

    r = await client.put(
        f"/api/boards/{board_id}/layout",
        headers=alice_auth,
        json={"version": 1, "items": [_item(app_id), _item(app_id, x=4)]},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BOARD_LAYOUT_INVALID"

    r = await client.put(
        f"/api/boards/{board_id}/layout",
        headers=alice_auth,
        json={"version": 1, "items": [_item(app_id, x=8, y=2)]},
    )
    assert r.status_code == 200
    saved = r.json()
    assert saved["version"] == 2
    assert saved["item_count"] == 1
    assert saved["layout"]["items"][0]["app_id"] == app_id

    # The first tab still holds version 1; its next save loses the race.
    r = await client.put(
        f"/api/boards/{board_id}/layout",
        headers=alice_auth,
        json={"version": 1, "items": []},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BOARD_CONFLICT"

    r = await client.patch(
        f"/api/boards/{board_id}",
        headers=alice_auth,
        json={"name": "Desk"},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Desk"
    assert r.json()["version"] == 2

    r = await client.delete(f"/api/boards/{board_id}", headers=alice_auth)
    assert r.status_code == 200
    assert r.json() == {"deleted": board_id}

    r = await client.get(f"/api/boards/{board_id}", headers=alice_auth)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "BOARD_NOT_FOUND"


async def test_layout_round_trips_locked_cells(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, srv, alice_auth, _bob_auth, agent_id = env_alice_bob_agent
    app_id = _app_id(srv, "alice", agent_id, "Tracker")

    r = await client.post("/api/boards", headers=alice_auth, json={"name": "Home"})
    board_id = r.json()["board_id"]

    r = await client.put(
        f"/api/boards/{board_id}/layout",
        headers=alice_auth,
        json={
            "version": 1,
            "items": [{**_item(app_id), "locked": True}],
        },
    )
    assert r.status_code == 200
    assert r.json()["layout"]["items"][0]["locked"] is True

    r = await client.get(f"/api/boards/{board_id}", headers=alice_auth)
    assert r.json()["layout"]["items"][0]["locked"] is True


async def test_board_is_private_to_its_owner(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, _srv, alice_auth, bob_auth, _agent_id = env_alice_bob_agent

    r = await client.post("/api/boards", headers=alice_auth, json={"name": "Home"})
    assert r.status_code == 201
    board_id = r.json()["board_id"]

    r = await client.get(f"/api/boards/{board_id}", headers=bob_auth)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "BOARD_FORBIDDEN"

    r = await client.get("/api/boards", headers=bob_auth)
    assert r.status_code == 200
    assert r.json() == []


async def test_layout_rejects_app_owned_by_someone_else(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, srv, alice_auth, _bob_auth, agent_id = env_alice_bob_agent
    bob_app = _app_id(srv, "bob", agent_id, "Bob tracker")

    r = await client.post("/api/boards", headers=alice_auth, json={"name": "Home"})
    board_id = r.json()["board_id"]

    r = await client.put(
        f"/api/boards/{board_id}/layout",
        headers=alice_auth,
        json={"version": 1, "items": [_item(bob_app)]},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BOARD_LAYOUT_INVALID"


async def test_board_name_validation(
    env_alice_bob_agent: tuple[httpx.AsyncClient, OctopServer, dict[str, str], dict[str, str], str],
) -> None:
    client, _srv, alice_auth, _bob_auth, _agent_id = env_alice_bob_agent

    r = await client.post("/api/boards", headers=alice_auth, json={"name": "   "})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BOARD_NAME_INVALID"
