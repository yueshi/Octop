"""Unit tests for light-app lifecycle, workspace files and ACL."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from deepagents.backends import FilesystemBackend
from octop_harness.backends import BackendWorkspace

from octop.infra.apps.models import parse_schema_payload
from octop.infra.apps.service import (
    _APP_LOCKS,
    FILE_PENDING,
    AppService,
    app_dir,
    app_path,
)
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.apps import LightAppRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.utils.ulid import new_ulid

COLUMNS: list[dict[str, object]] = [
    {"name": "title", "type": "string", "required": True},
    {"name": "qty", "type": "integer"},
    {"name": "status", "type": "enum", "values": ["new", "doing", "done"]},
]


@dataclass
class World:
    service: AppService
    repo: LightAppRepo
    agent_repo: AgentRepo
    user_repo: UserRepo
    workspace: BackendWorkspace
    agent_id: str
    alice_id: int
    bob_id: int


class _Workspaces:
    def __init__(self, agent_id: str, workspace: BackendWorkspace) -> None:
        self._agent_id = agent_id
        self._workspace = workspace

    def workspace_for_agent(self, agent_id: str) -> BackendWorkspace | None:
        return self._workspace if agent_id == self._agent_id else None


@pytest.fixture
def world(tmp_path: Path) -> World:
    db = SqlitePool(tmp_path / "octop.db")
    run_migrations(db)
    user_repo = UserRepo(db)
    alice_id = user_repo.create(username="alice", password_hash="h", role="user")
    bob_id = user_repo.create(username="bob", password_hash="h", role="user")
    agent_repo = AgentRepo(db)
    agent_id = new_ulid()
    agent_repo.create(agent_id=agent_id, user_id=alice_id, name="bot")
    root = tmp_path / "ws"
    root.mkdir()
    workspace = BackendWorkspace(FilesystemBackend(root_dir=str(root), virtual_mode=True), root)
    repo = LightAppRepo(db)
    return World(
        service=AppService(
            app_repo=repo,
            agent_repo=agent_repo,
            user_repo=user_repo,
            workspaces=_Workspaces(agent_id, workspace),
        ),
        repo=repo,
        agent_repo=agent_repo,
        user_repo=user_repo,
        workspace=workspace,
        agent_id=agent_id,
        alice_id=alice_id,
        bob_id=bob_id,
    )


def _assert_code(exc: pytest.ExceptionInfo[OctopError], code: ErrorCode) -> None:
    assert exc.value.code == code


async def _create(world: World, *, name: str = "Tracker") -> str:
    row = await world.service.create(
        agent_id=world.agent_id,
        owner_user_id=world.alice_id,
        name=name,
        description="things",
        columns=COLUMNS,
    )
    return row.app_id


async def test_create_writes_schema_csv_and_manifest(world: World) -> None:
    row = await world.service.create(
        agent_id=world.agent_id,
        owner_user_id=world.alice_id,
        name="  Tracker  ",
        description=" things ",
        columns=COLUMNS,
    )
    assert row.app_id.startswith("app_")
    assert row.name == "Tracker"
    assert row.description == "things"
    assert row.row_count == 0

    schema = parse_schema_payload(
        await world.workspace.aread_text(app_path(row.app_id, "schema.json"))
    )
    assert schema.names == ("title", "qty", "status")
    assert await world.workspace.aread_text(app_path(row.app_id, "data.csv")) == (
        "title,qty,status\n"
    )
    manifest = json.loads(await world.workspace.aread_text(app_path(row.app_id, "manifest.json")))
    assert manifest["app_id"] == row.app_id
    assert manifest["owner_user_id"] == world.alice_id
    assert await world.workspace.aexists(app_dir(row.app_id))


async def test_create_rejects_invalid_name_or_schema(world: World) -> None:
    with pytest.raises(OctopError) as exc:
        await world.service.create(
            agent_id=world.agent_id,
            owner_user_id=world.alice_id,
            name="  ",
            columns=COLUMNS,
        )
    _assert_code(exc, ErrorCode.APP_NAME_INVALID)

    with pytest.raises(OctopError) as exc:
        await world.service.create(
            agent_id=world.agent_id,
            owner_user_id=world.alice_id,
            name="x",
            columns=[{"name": "ok", "type": "blob"}],
        )
    _assert_code(exc, ErrorCode.APP_SCHEMA_INVALID)
    assert world.repo.count_for_owner(world.alice_id) == 0


async def test_append_and_read_rows(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)

    index, stored = await world.service.append_row(row, {"title": "A", "qty": "2", "status": "new"})
    assert index == 0
    assert stored == {"title": "A", "qty": "2", "status": "new"}
    await world.service.append_row(row, {"title": "B", "status": "doing"})

    schema, rows, total = await world.service.read_rows(row)
    assert schema.names == ("title", "qty", "status")
    assert total == 2
    assert rows[0]["title"] == "A"
    assert rows[1]["qty"] == ""

    _, page, _ = await world.service.read_rows(row, limit=1, offset=1)
    assert [r["title"] for r in page] == ["B"]
    assert world.repo.get(app_id).row_count == 2


async def test_append_rejects_invalid_row(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    with pytest.raises(OctopError) as exc:
        await world.service.append_row(row, {"title": "A", "nope": "1"})
    _assert_code(exc, ErrorCode.APP_ROW_INVALID)
    with pytest.raises(OctopError) as exc:
        await world.service.append_row(row, {"qty": "1"})
    _assert_code(exc, ErrorCode.APP_ROW_INVALID)
    assert world.repo.get(app_id).row_count == 0


async def test_add_columns_preserves_rows(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    await world.service.append_row(row, {"title": "A", "status": "new"})

    schema = await world.service.add_columns(row, [{"name": "notes", "type": "text"}])
    assert schema.version == 2
    assert schema.names == ("title", "qty", "status", "notes")
    _, rows, _ = await world.service.read_rows(row)
    assert rows[0]["title"] == "A"
    assert rows[0]["notes"] == ""

    with pytest.raises(OctopError) as exc:
        await world.service.add_columns(row, [{"name": "owner", "required": True}])
    _assert_code(exc, ErrorCode.APP_SCHEMA_INVALID)


async def test_write_and_read_html(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    assert await world.service.has_html(row) is False
    assert await world.service.read_html(row) is None

    size = await world.service.write_html(row, "<h1>Hello</h1>")
    assert size == len("<h1>Hello</h1>")
    assert await world.service.has_html(row) is True
    assert await world.service.read_html(row) == "<h1>Hello</h1>"

    with pytest.raises(OctopError) as exc:
        await world.service.write_html(row, "   ")
    _assert_code(exc, ErrorCode.APP_HTML_INVALID)


async def test_file_stats_track_the_workspace_files(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    assert row.html_bytes == 0
    assert row.data_bytes > 0

    await world.service.write_html(row, "<h1>Hello</h1>")
    row = world.service.get(app_id)
    assert row.html_bytes == len("<h1>Hello</h1>")

    await world.service.append_row(row, {"title": "A", "qty": "1"})
    row = world.service.get(app_id)
    assert row.data_bytes > 0
    view = world.service.view(row, "edit_structure")
    assert view.has_html is True
    assert view.html_bytes == row.html_bytes
    assert view.data_bytes == row.data_bytes


async def test_list_files_reports_the_app_workspace_files(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    await world.service.write_html(row, "<h1>Hello</h1>")

    files = await world.service.list_files(row)
    assert [item.name for item in files] == [
        "app.html",
        "data.csv",
        "schema.json",
        "manifest.json",
    ]
    by_name = {item.name: item for item in files}
    assert by_name["app.html"].size == len("<h1>Hello</h1>")
    assert by_name["data.csv"].size and by_name["data.csv"].size > 0
    assert by_name["data.csv"].path == f"apps/{app_id}/data.csv"


async def test_list_views_backfills_the_cached_interface_size(world: World) -> None:
    """Apps written before the size columns existed still report their UI."""
    app_id = await _create(world)
    row = world.service.get(app_id)
    await world.service.write_html(row, "<h1>Hello</h1>")
    # Simulate an app created before the file-size columns existed.
    world.repo.set_file_stats(app_id, html_bytes=0, data_bytes=0)
    assert world.service.get(app_id).html_bytes == 0

    views = await world.service.list_views_with_stats(world.alice_id, is_admin=False)
    assert [view.app_id for view in views] == [app_id]
    assert views[0].html_bytes == len("<h1>Hello</h1>")
    assert views[0].data_bytes > 0
    assert views[0].has_html is True
    # The repaired sizes are cached, so later listings do not read the workspace.
    stored = world.service.get(app_id)
    assert stored.html_bytes == len("<h1>Hello</h1>")
    assert stored.data_bytes == views[0].data_bytes


async def test_permissions_and_collaborators(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)

    assert world.service.permission_for(row, world.alice_id, is_admin=False) == "edit_structure"
    assert world.service.permission_for(row, world.bob_id, is_admin=False) is None
    assert world.service.permission_for(row, world.bob_id, is_admin=True) == "edit_structure"
    with pytest.raises(OctopError) as exc:
        world.service.require(row, world.bob_id, "view", is_admin=False)
    _assert_code(exc, ErrorCode.APP_FORBIDDEN)

    world.service.set_collaborators(row, [(world.bob_id, "view")])
    assert world.service.permission_for(row, world.bob_id, is_admin=False) == "view"
    assert world.service.require(row, world.bob_id, "view", is_admin=False) == "view"
    with pytest.raises(OctopError) as exc:
        world.service.require(row, world.bob_id, "edit_data", is_admin=False)
    _assert_code(exc, ErrorCode.APP_FORBIDDEN)

    world.service.set_collaborators(row, [(world.bob_id, "edit_data")])
    assert world.service.require(row, world.bob_id, "edit_data", is_admin=False) == "edit_data"
    collaborators = world.service.list_collaborators(row)
    assert [(c.user_id, c.permission) for c in collaborators] == [(world.bob_id, "edit_data")]

    with pytest.raises(OctopError) as exc:
        world.service.set_collaborators(row, [(world.alice_id, "view")])
    _assert_code(exc, ErrorCode.APP_SHARE_INVALID)
    with pytest.raises(OctopError) as exc:
        world.service.set_collaborators(row, [(world.bob_id, "view"), (world.bob_id, "view")])
    _assert_code(exc, ErrorCode.APP_SHARE_INVALID)
    with pytest.raises(OctopError) as exc:
        world.service.set_collaborators(row, [(world.bob_id, "edit_structure")])
    _assert_code(exc, ErrorCode.APP_SHARE_INVALID)


async def test_list_for_user_and_agent(world: World) -> None:
    first = await _create(world, name="First")
    second = await _create(world, name="Second")
    world.service.set_collaborators(world.service.get(first), [(world.bob_id, "view")])

    owned = world.service.list_for_user(world.alice_id, is_admin=False)
    assert {row.app_id for row, _ in owned} == {first, second}
    assert all(permission == "edit_structure" for _, permission in owned)

    shared = world.service.list_for_user(world.bob_id, is_admin=False)
    assert [(row.app_id, permission) for row, permission in shared] == [(first, "view")]

    everything = world.service.list_for_user(world.bob_id, is_admin=True)
    assert {row.app_id for row, _ in everything} == {first, second}

    assert {row.app_id for row in world.service.list_for_agent(world.agent_id, world.alice_id)} == {
        first,
        second,
    }
    assert world.service.list_for_agent(world.agent_id, world.bob_id) == []
    assert world.service.is_agent_owner(world.agent_id, world.alice_id) is True
    assert world.service.is_agent_owner(world.agent_id, world.bob_id) is False


async def test_delete_removes_metadata_and_workspace_files(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    await world.service.write_html(row, "<p>x</p>")

    await world.service.delete(row)
    assert world.repo.get(app_id) is None
    assert await world.workspace.aexists(app_dir(app_id)) is False


async def test_update_metadata(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    updated = await world.service.update_metadata(row, name="Renamed", description="")
    assert updated.name == "Renamed"
    assert updated.description == ""


async def test_manifest_refreshes_after_mutations(world: World) -> None:
    app_id = await _create(world)
    await world.service.update_metadata(
        world.service.get(app_id), name="Renamed", description="new"
    )
    manifest = json.loads(await world.workspace.aread_text(app_path(app_id, "manifest.json")))
    assert manifest["name"] == "Renamed"
    assert manifest["description"] == "new"

    await world.service.append_row(world.service.get(app_id), {"title": "A", "status": "new"})
    manifest = json.loads(await world.workspace.aread_text(app_path(app_id, "manifest.json")))
    assert manifest["row_count"] == 1
    assert manifest["schema_version"] == 1

    await world.service.add_columns(world.service.get(app_id), [{"name": "notes"}])
    manifest = json.loads(await world.workspace.aread_text(app_path(app_id, "manifest.json")))
    assert manifest["schema_version"] == 2


async def test_row_count_reconciles_with_data_csv(world: World) -> None:
    app_id = await _create(world)
    await world.service.append_row(world.service.get(app_id), {"title": "A", "status": "new"})
    await world.workspace.awrite_text(
        app_path(app_id, "data.csv"),
        "title,qty,status\nA,,new\nB,,done\n",
    )
    assert world.repo.get(app_id).row_count == 1

    _, rows, total = await world.service.read_rows(world.service.get(app_id))
    assert total == 2
    assert [item["title"] for item in rows] == ["A", "B"]
    assert world.repo.get(app_id).row_count == 2


async def test_delete_releases_lock_and_rejects_stale_writes(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    await world.service.delete(row)
    assert app_id not in _APP_LOCKS
    assert await world.workspace.aexists(app_dir(app_id)) is False

    with pytest.raises(OctopError) as exc:
        await world.service.write_html(row, "<p>x</p>")
    _assert_code(exc, ErrorCode.APP_NOT_FOUND)
    assert await world.workspace.aexists(app_dir(app_id)) is False

    with pytest.raises(OctopError) as exc:
        await world.service.append_row(row, {"title": "A", "status": "new"})
    _assert_code(exc, ErrorCode.APP_NOT_FOUND)
    with pytest.raises(OctopError) as exc:
        await world.service.add_columns(row, [{"name": "notes"}])
    _assert_code(exc, ErrorCode.APP_NOT_FOUND)
    assert app_id not in _APP_LOCKS
    assert await world.workspace.aexists(app_dir(app_id)) is False


async def test_append_aborts_when_data_csv_is_unreadable(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    await world.service.append_row(row, {"title": "A", "status": "new"})
    await world.workspace.adelete(app_path(app_id, "data.csv"))

    with pytest.raises(OctopError) as exc:
        await world.service.append_row(row, {"title": "B", "status": "new"})
    _assert_code(exc, ErrorCode.INTERNAL_ERROR)
    assert await world.workspace.aexists(app_path(app_id, "data.csv")) is False

    with pytest.raises(OctopError) as exc:
        await world.service.read_rows(row)
    _assert_code(exc, ErrorCode.INTERNAL_ERROR)


async def test_create_cleans_up_partial_workspace_files(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = world.workspace.awrite_text
    calls = {"count": 0}

    async def failing_write(path: str, content: str) -> None:
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("disk full")
        await original(path, content)

    monkeypatch.setattr(world.workspace, "awrite_text", failing_write)
    with pytest.raises(RuntimeError):
        await _create(world)
    assert world.repo.count_for_owner(world.alice_id) == 0
    listing = await world.workspace.als("apps")
    assert listing is None or not listing.entries


async def test_append_staging_failure_preserves_existing_rows(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    await world.service.append_row(row, {"title": "A", "status": "new"})

    original = world.workspace.awrite_text

    async def failing_write(path: str, content: str) -> None:
        if path.endswith("data.csv.new"):
            raise RuntimeError("disk full")
        await original(path, content)

    monkeypatch.setattr(world.workspace, "awrite_text", failing_write)
    with pytest.raises(RuntimeError):
        await world.service.append_row(row, {"title": "B", "status": "new"})
    monkeypatch.setattr(world.workspace, "awrite_text", original)

    _, rows, total = await world.service.read_rows(row)
    assert total == 1
    assert rows[0]["title"] == "A"
    assert await world.workspace.aexists(app_path(app_id, FILE_PENDING)) is False


async def test_add_columns_recovers_interrupted_promotion(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    await world.service.append_row(row, {"title": "A", "status": "new"})

    original = world.workspace.awrite_text

    async def failing_write(path: str, content: str) -> None:
        # Fail while copying the staged data over the live file; schema.json
        # has already been promoted at this point.
        if path.endswith("/data.csv"):
            raise RuntimeError("backend 5xx")
        await original(path, content)

    monkeypatch.setattr(world.workspace, "awrite_text", failing_write)
    with pytest.raises(RuntimeError):
        await world.service.add_columns(row, [{"name": "notes"}])
    monkeypatch.setattr(world.workspace, "awrite_text", original)

    schema, rows, total = await world.service.read_rows(world.service.get(app_id))
    assert schema.version == 2
    assert schema.names == ("title", "qty", "status", "notes")
    assert total == 1
    assert rows[0]["title"] == "A"
    assert await world.workspace.aexists(app_path(app_id, FILE_PENDING)) is False
    assert await world.workspace.aexists(app_path(app_id, "data.csv.new")) is False


async def test_pending_journal_rejects_paths_outside_the_app(world: World) -> None:
    app_id = await _create(world)
    await world.workspace.awrite_text("SOUL.md", "secret")
    await world.workspace.awrite_text(
        app_path(app_id, FILE_PENDING),
        json.dumps({"files": ["../SOUL.md"]}),
    )

    await world.service.read_rows(world.service.get(app_id))

    assert await world.workspace.aread_text("SOUL.md") == "secret"
    assert await world.workspace.aexists(app_path(app_id, FILE_PENDING)) is False


async def test_set_shares_resolves_and_validates_accounts(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)

    views = world.service.set_shares(row, [("bob", "view")])
    assert [(view.username, view.permission) for view in views] == [("bob", "view")]

    with pytest.raises(OctopError) as exc:
        world.service.set_shares(row, [("nobody", "view")])
    _assert_code(exc, ErrorCode.APP_SHARE_INVALID)

    with pytest.raises(OctopError) as exc:
        world.service.set_shares(row, [("bob", "view"), ("bob", "edit_data")])
    _assert_code(exc, ErrorCode.APP_SHARE_INVALID)

    with pytest.raises(OctopError) as exc:
        world.service.set_shares(row, [("alice", "view")])
    _assert_code(exc, ErrorCode.APP_SHARE_INVALID)

    world.user_repo.set_disabled(world.bob_id, True)
    with pytest.raises(OctopError) as exc:
        world.service.set_shares(row, [("bob", "view")])
    _assert_code(exc, ErrorCode.APP_SHARE_INVALID)
