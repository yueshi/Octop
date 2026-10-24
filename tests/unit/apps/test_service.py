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
    db: SqlitePool
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
        db=db,
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


async def test_create_registers_the_main_table(world: World) -> None:
    app_id = await _create(world)
    tables = world.repo.list_tables(app_id)
    assert [(item.table_key, item.row_count) for item in tables] == [("main", 0)]
    assert world.service.list_tables(world.service.get(app_id))[0].is_main is True


async def test_multi_table_rows_are_isolated(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    table = await world.service.create_table(
        row,
        table_key="orders",
        label="Orders",
        columns=[{"name": "item", "type": "string", "required": True}],
    )
    assert (table.table_key, table.label, table.row_count, table.is_main) == (
        "orders",
        "Orders",
        0,
        False,
    )

    await world.service.append_row(row, {"title": "A", "status": "new"})
    await world.service.append_row(row, {"item": "cup"}, table="orders")
    await world.service.append_row(row, {"item": "pen"}, table="orders")

    _, main_rows, main_total = await world.service.read_rows(world.service.get(app_id))
    assert main_total == 1
    assert main_rows[0]["title"] == "A"

    _, order_rows, order_total = await world.service.read_rows(
        world.service.get(app_id), table="orders"
    )
    assert order_total == 2
    assert [item["item"] for item in order_rows] == ["cup", "pen"]

    stored = world.repo.get(app_id)
    assert stored.row_count == 3  # light_apps carries the aggregate
    assert world.repo.get_table(app_id, "orders").row_count == 2

    schema = await world.service.read_schema(world.service.get(app_id), "orders")
    assert schema.names == ("item",)

    names = [item.name for item in await world.service.list_files(world.service.get(app_id))]
    assert "tables/orders.csv" in names
    assert "tables/orders.schema.json" in names

    manifest = json.loads(await world.workspace.aread_text(app_path(app_id, "manifest.json")))
    assert [item["key"] for item in manifest["tables"]] == ["main", "orders"]
    assert manifest["tables"][1]["schema_version"] == 1
    assert manifest["tables"][1]["row_count"] == 2


async def test_add_columns_on_an_extra_table(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    await world.service.create_table(row, table_key="orders", columns=[{"name": "item"}])
    await world.service.append_row(row, {"item": "cup"}, table="orders")

    schema = await world.service.add_columns(
        row, [{"name": "qty", "type": "integer"}], table="orders"
    )
    assert schema.version == 2
    _, rows, _ = await world.service.read_rows(world.service.get(app_id), table="orders")
    assert rows == [{"item": "cup", "qty": ""}]
    # The main table keeps its own schema version.
    assert (await world.service.read_schema(world.service.get(app_id))).version == 1
    manifest = json.loads(await world.workspace.aread_text(app_path(app_id, "manifest.json")))
    versions = {item["key"]: item["schema_version"] for item in manifest["tables"]}
    assert versions == {"main": 1, "orders": 2}


async def test_table_keys_are_validated(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    for key in ("main", "bad key", "tables", "1abc"):
        with pytest.raises(OctopError) as exc:
            await world.service.create_table(row, table_key=key, columns=[{"name": "x"}])
        _assert_code(exc, ErrorCode.APP_SCHEMA_INVALID)

    await world.service.create_table(row, table_key="orders", columns=[{"name": "x"}])
    with pytest.raises(OctopError) as exc:
        await world.service.create_table(row, table_key="orders", columns=[{"name": "y"}])
    _assert_code(exc, ErrorCode.APP_SCHEMA_INVALID)

    with pytest.raises(OctopError) as exc:
        await world.service.read_rows(row, table="missing")
    _assert_code(exc, ErrorCode.APP_SCHEMA_INVALID)


async def test_legacy_app_backfills_the_main_table_on_write(world: World) -> None:
    app_id = await _create(world)
    # Simulate an app created before the table registry existed.
    with world.db.transaction() as conn:
        conn.execute("DELETE FROM light_app_tables WHERE app_id = ?", (app_id,))
    assert world.repo.list_tables(app_id) == []

    await world.service.append_row(world.service.get(app_id), {"title": "A", "status": "new"})
    tables = world.repo.list_tables(app_id)
    assert [(item.table_key, item.row_count) for item in tables] == [("main", 1)]
    assert world.repo.get(app_id).row_count == 1


async def test_import_rows_appends_skips_and_counts(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    await world.service.append_row(row, {"title": "A", "qty": 1, "status": "new"})

    result = await world.service.import_rows(
        row,
        [
            {"title": "B", "qty": "2", "status": "done"},
            {"title": "C", "qty": "not-a-number", "status": "new"},  # invalid qty
            {"title": "", "qty": "3", "status": "new"},  # missing required title
        ],
    )
    assert (result.imported, result.skipped) == (1, 2)
    assert len(result.errors) == 2
    assert "row 2" in result.errors[0]

    _, rows, total = await world.service.read_rows(world.service.get(app_id))
    assert total == 2
    assert rows[-1]["title"] == "B"
    assert world.repo.get_table(app_id, "main").row_count == 2
    assert world.repo.get(app_id).row_count == 2


async def test_import_rows_replace_overwrites_table(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    await world.service.append_row(row, {"title": "A", "status": "new"})

    result = await world.service.import_rows(
        row,
        [{"title": "X", "qty": "9", "status": "done"}],
        mode="replace",
    )
    assert (result.imported, result.skipped) == (1, 0)
    _, rows, total = await world.service.read_rows(world.service.get(app_id))
    assert total == 1
    assert rows[0]["title"] == "X"


async def test_import_rows_replace_rejects_all_invalid(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    await world.service.append_row(row, {"title": "A", "status": "new"})

    with pytest.raises(OctopError) as exc:
        await world.service.import_rows(
            row,
            [{"title": "", "qty": "1", "status": "new"}],
            mode="replace",
        )
    _assert_code(exc, ErrorCode.APP_ROW_INVALID)
    _, _rows, total = await world.service.read_rows(world.service.get(app_id))
    assert total == 1  # table left unchanged


async def test_import_rows_maps_source_columns(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    result = await world.service.import_rows(
        row,
        [{"item": "cup", "count": "3"}],
        mapping={"item": "title", "count": "qty"},
    )
    assert result.imported == 1
    _, rows, _total = await world.service.read_rows(world.service.get(app_id))
    assert rows[0] == {"title": "cup", "qty": "3", "status": ""}

    with pytest.raises(OctopError) as exc:
        await world.service.import_rows(row, [{"a": "b"}], mapping={"a": "nope"})
    _assert_code(exc, ErrorCode.APP_SCHEMA_INVALID)

    with pytest.raises(OctopError) as exc:
        await world.service.import_rows(row, [{"a": "b"}], mode="merge")
    _assert_code(exc, ErrorCode.APP_ROW_INVALID)


async def test_import_from_workspace_file(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    await world.workspace.awrite_text("inbound/exports/items.csv", "title,qty,status\ncup,3,new\n")

    result = await world.service.import_from_file(row, path="inbound/exports/items.csv")
    assert (result.imported, result.skipped) == (1, 0)
    _, rows, total = await world.service.read_rows(world.service.get(app_id))
    assert total == 1 and rows[0]["title"] == "cup"

    with pytest.raises(OctopError) as exc:
        await world.service.import_from_file(row, path="inbound/exports/missing.csv")
    _assert_code(exc, ErrorCode.APP_ROW_INVALID)

    for bad in (".octop/auth/tokens.json", "a/../b.csv", "../escape.csv", ""):
        with pytest.raises(OctopError) as exc:
            await world.service.import_from_file(row, path=bad)
        _assert_code(exc, ErrorCode.APP_ROW_INVALID)


async def test_save_import_file_stores_under_inbound_imports(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    path = await world.service.save_import_file(row, filename="..\\my data.csv", data=b"title\nA\n")
    assert path.startswith("inbound/imports/")
    assert path.endswith("-my data.csv")
    assert await world.workspace.aread_text(path) == "title\nA\n"


async def test_save_source_validation(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)

    view = world.service.save_source(
        row,
        source_id="src_1",
        table_key="main",
        kind="workspace_file",
        mode="append",
        params={"path": "inbound/imports/a.csv"},
        created_by=world.alice_id,
    )
    assert view.kind == "workspace_file"
    assert view.params == {"path": "inbound/imports/a.csv"}
    assert view.schedule_seconds == 0

    # Table clash on create.
    with pytest.raises(OctopError) as exc:
        world.service.save_source(
            row,
            source_id="src_2",
            table_key="main",
            kind="workspace_file",
            mode="append",
            params={"path": "b.csv"},
            created_by=world.alice_id,
        )
    _assert_code(exc, ErrorCode.APP_SOURCE_INVALID)

    bad_cases: list[dict[str, object]] = [
        {"kind": "rss"},
        {"mode": "upsert"},
        {"schedule_seconds": -1},
        {"schedule_seconds": 31 * 24 * 3600 + 1},
        {"params": {"path": "../escape.csv"}},
        {"kind": "external", "params": {}},
        {"kind": "external", "params": {"instance_id": "i1"}},
        {"params": {"path": ".octop/auth/tokens.json"}},
    ]
    for override in bad_cases:
        kwargs: dict[str, object] = {
            "table_key": "main",
            "kind": "workspace_file",
            "mode": "append",
            "params": {"path": "a.csv"},
            "created_by": world.alice_id,
        }
        kwargs.update(override)
        with pytest.raises(OctopError) as exc:
            world.service.save_source(
                row,
                source_id="src_1",  # type: ignore[arg-type]
                **kwargs,  # type: ignore[arg-type]
            )
        _assert_code(exc, ErrorCode.APP_SOURCE_INVALID)

    # Unknown table.
    with pytest.raises(OctopError) as exc:
        world.service.save_source(
            row,
            source_id="src_1",
            table_key="nope",
            kind="workspace_file",
            mode="append",
            params={"path": "a.csv"},
            created_by=world.alice_id,
        )
    _assert_code(exc, ErrorCode.APP_SCHEMA_INVALID)

    assert len(world.service.list_sources(row)) == 1


async def test_run_source_workspace_file_records_state(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    await world.workspace.awrite_text("inbound/imports/data.csv", "title,qty,status\nmug,2,new\n")

    world.service.save_source(
        row,
        source_id="src_1",
        table_key="main",
        kind="workspace_file",
        mode="append",
        schedule_seconds=60,
        params={"path": "inbound/imports/data.csv"},
        created_by=world.alice_id,
    )
    result = await world.service.run_source(row, "src_1")
    assert (result.imported, result.skipped) == (1, 0)
    view = world.service.get_source(row, "src_1")
    assert view.last_status == "ok"
    assert view.last_row_count == 1
    assert view.last_error is None

    # A failing run records the error and re-raises.
    await world.workspace.adelete("inbound/imports/data.csv")
    with pytest.raises(OctopError):
        await world.service.run_source(row, "src_1")
    view = world.service.get_source(row, "src_1")
    assert view.last_status == "error"
    assert view.last_error

    # Unknown binding.
    with pytest.raises(OctopError) as exc:
        await world.service.run_source(row, "src_x")
    _assert_code(exc, ErrorCode.APP_NOT_FOUND)


async def test_run_source_external_uses_runner(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    calls: list[dict[str, object]] = []

    class _Runner:
        def run(self, *, instance_id: str, tool: str, args: dict[str, object]) -> str:
            calls.append({"instance_id": instance_id, "tool": tool, "args": args})
            return json.dumps(
                {
                    "data": {
                        "items": [
                            {"name": "cup", "count": 3},
                            {"name": "mug", "count": "not-a-number"},
                        ]
                    }
                }
            )

    from octop.infra.apps.service import AppService as _Svc

    runner_service = _Svc(
        app_repo=world.repo,
        agent_repo=world.agent_repo,
        user_repo=world.user_repo,
        workspaces=world.service._workspaces,  # type: ignore[attr-defined]
        external_tool_runner=_Runner(),  # type: ignore[arg-type]
    )

    runner_service.save_source(
        row,
        source_id="src_1",
        table_key="main",
        kind="external",
        mode="append",
        mapping={"name": "title", "count": "qty"},
        params={
            "instance_id": "conn1",
            "tool": "search",
            "args": {"q": "cup"},
            "result_path": "data.items",
        },
        created_by=world.alice_id,
    )
    result = await runner_service.run_source(row, "src_1")
    # The invalid qty row is skipped, the valid one imported.
    assert (result.imported, result.skipped) == (1, 1)
    assert calls == [{"instance_id": "conn1", "tool": "search", "args": {"q": "cup"}}]
    _, rows, total = await world.service.read_rows(world.service.get(app_id))
    assert total == 1 and rows[0]["title"] == "cup" and rows[0]["qty"] == "3"

    view = runner_service.get_source(row, "src_1")
    assert view.last_status == "ok" and view.last_row_count == 1

    class _Failing:
        def run(self, *, instance_id: str, tool: str, args: dict[str, object]) -> str:
            raise RuntimeError("network down")

    failing_service = _Svc(
        app_repo=world.repo,
        agent_repo=world.agent_repo,
        user_repo=world.user_repo,
        workspaces=world.service._workspaces,  # type: ignore[attr-defined]
        external_tool_runner=_Failing(),  # type: ignore[arg-type]
    )
    with pytest.raises(OctopError) as exc:
        await failing_service.run_source(row, "src_1")
    _assert_code(exc, ErrorCode.APP_SOURCE_INVALID)
    view = runner_service.get_source(row, "src_1")
    assert view.last_status == "error" and "network down" in (view.last_error or "")


def test_extract_records_shapes() -> None:
    from octop.infra.apps.service import _extract_records

    assert _extract_records('[{"a": 1}, {"a": 2}]', "") == [{"a": "1"}, {"a": "2"}]
    assert _extract_records('{"data": {"items": [{"a": 1}]}}', "data.items") == [{"a": "1"}]

    for output, path in (
        ("not json", ""),
        ('{"data": []}', "data.items.missing"),
        ('{"data": "nope"}', "data.items"),
        ('[{"a": 1}, "str"]', ""),
    ):
        with pytest.raises(OctopError) as exc:
            _extract_records(output, path)
        _assert_code(exc, ErrorCode.APP_SOURCE_INVALID)


async def test_run_source_backoff_and_reset(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    await world.workspace.awrite_text("inbound/imports/b.csv", "title,qty,status\nmug,1,new\n")
    world.service.save_source(
        row,
        source_id="s1",
        table_key="main",
        kind="workspace_file",
        mode="append",
        schedule_seconds=60,
        params={"path": "inbound/imports/b.csv"},
        created_by=world.alice_id,
    )

    # Success re-arms the normal interval and resets the failure counter.
    await world.service.run_source(row, "s1")
    item = world.repo.get_source(app_id, "s1")
    assert item is not None
    assert item.fail_count == 0
    assert item.next_run_at is not None and item.last_run_at is not None
    assert item.next_run_at - item.last_run_at == 60

    # Failures back off exponentially: 1x, then 2x.
    await world.workspace.adelete("inbound/imports/b.csv")
    for expected_delay in (60, 120):
        with pytest.raises(OctopError):
            await world.service.run_source(row, "s1")
        item = world.repo.get_source(app_id, "s1")
        assert item is not None
        assert item.fail_count == expected_delay // 60
        assert item.next_run_at is not None and item.last_run_at is not None
        assert item.next_run_at - item.last_run_at == expected_delay

    # Backoff is capped at MAX_BACKOFF_SECONDS.
    world.service.delete_source(row, "s1")
    world.service.save_source(
        row,
        source_id="s2",
        table_key="main",
        kind="workspace_file",
        mode="append",
        schedule_seconds=70000,
        params={"path": "inbound/imports/b.csv"},
        created_by=world.alice_id,
    )
    for expected_delay in (70000, 24 * 3600):
        with pytest.raises(OctopError):
            await world.service.run_source(row, "s2")
        item = world.repo.get_source(app_id, "s2")
        assert item is not None
        assert item.next_run_at is not None and item.last_run_at is not None
        assert item.next_run_at - item.last_run_at == expected_delay
