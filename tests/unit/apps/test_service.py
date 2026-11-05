"""Unit tests for light-app lifecycle, workspace files and ACL."""

from __future__ import annotations

import io
import json
import zipfile
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


def _xlsx_bytes(sheets: dict[str, list[list[object]]]) -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets.items():
        sheet = workbook.create_sheet(title=name)
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


@dataclass
class World:
    service: AppService
    repo: LightAppRepo
    db: SqlitePool
    agent_repo: AgentRepo
    user_repo: UserRepo
    workspace: BackendWorkspace
    other_workspace: BackendWorkspace
    agent_id: str
    other_agent_id: str
    alice_id: int
    bob_id: int


class _Workspaces:
    def __init__(self, workspaces: dict[str, BackendWorkspace]) -> None:
        self._workspaces = workspaces

    def workspace_for_agent(self, agent_id: str) -> BackendWorkspace | None:
        return self._workspaces.get(agent_id)


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
    other_agent_id = new_ulid()
    agent_repo.create(agent_id=other_agent_id, user_id=alice_id, name="other")
    root = tmp_path / "ws"
    root.mkdir()
    workspace = BackendWorkspace(FilesystemBackend(root_dir=str(root), virtual_mode=True), root)
    other_root = tmp_path / "ws-other"
    other_root.mkdir()
    other_workspace = BackendWorkspace(
        FilesystemBackend(root_dir=str(other_root), virtual_mode=True), other_root
    )
    repo = LightAppRepo(db)
    return World(
        service=AppService(
            app_repo=repo,
            agent_repo=agent_repo,
            user_repo=user_repo,
            workspaces=_Workspaces({agent_id: workspace, other_agent_id: other_workspace}),
        ),
        repo=repo,
        db=db,
        agent_repo=agent_repo,
        user_repo=user_repo,
        workspace=workspace,
        other_workspace=other_workspace,
        agent_id=agent_id,
        other_agent_id=other_agent_id,
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


def _make_legacy(world: World, app_id: str) -> None:
    """Put an app back on CSV storage to exercise the migration-era path."""
    world.repo.set_storage_version(app_id, 1)


def _block_migration(monkeypatch: pytest.MonkeyPatch, world: World) -> None:
    async def _no_migrate(row: object) -> bool:
        return False

    monkeypatch.setattr(world.service, "_ensure_db_rows", _no_migrate)


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
    _make_legacy(world, app_id)
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


async def test_row_count_reconciles_with_data_csv(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_id = await _create(world)
    _make_legacy(world, app_id)
    _block_migration(monkeypatch, world)
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


async def test_append_aborts_when_data_csv_is_unreadable(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_id = await _create(world)
    _make_legacy(world, app_id)
    _block_migration(monkeypatch, world)
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
    _make_legacy(world, app_id)
    _block_migration(monkeypatch, world)
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
    _make_legacy(world, app_id)
    _block_migration(monkeypatch, world)
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
    _make_legacy(world, app_id)
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


async def test_source_upsert_key_keeps_scheduled_runs_idempotent(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    await world.workspace.awrite_text(
        "inbound/imports/month.csv",
        "title,qty,status\n2026-11,1,new\n",
    )
    world.service.save_source(
        row,
        source_id="src_month",
        table_key="main",
        kind="workspace_file",
        mode="append",
        schedule_seconds=86400,
        params={"path": "inbound/imports/month.csv", "upsert_key": ["title"]},
        created_by=world.alice_id,
    )
    assert world.service.get_source(row, "src_month").params["upsert_key"] == ["title"]

    first = await world.service.run_source(row, "src_month")
    assert (first.imported, first.updated) == (1, 0)

    await world.workspace.awrite_text(
        "inbound/imports/month.csv",
        "title,qty,status\n2026-11,2,done\n",
    )
    second = await world.service.run_source(row, "src_month")
    assert (second.imported, second.updated) == (0, 1)

    _schema, rows, total = await world.service.read_rows(world.service.get(app_id))
    assert total == 1
    assert rows == [{"title": "2026-11", "qty": "2", "status": "done"}]


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
    assert result.failed_rows == [
        {
            "row": 2,
            "values": {"title": "mug", "qty": "not-a-number"},
            "error": result.failed_rows[0]["error"],
        }
    ]
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


async def test_external_source_cursor_advances_between_runs(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    calls: list[dict[str, object]] = []

    class _Runner:
        def run(self, *, instance_id: str, tool: str, args: dict[str, object]) -> str:
            del instance_id, tool
            calls.append(dict(args))
            return json.dumps(
                {
                    "data": {
                        "items": [{"name": "cup", "count": 3}],
                        "next": "2026-11-02",
                    }
                }
            )

    from octop.infra.apps.service import AppService as _Svc

    service = _Svc(
        app_repo=world.repo,
        agent_repo=world.agent_repo,
        user_repo=world.user_repo,
        workspaces=world.service._workspaces,  # type: ignore[attr-defined]
        external_tool_runner=_Runner(),  # type: ignore[arg-type]
    )
    service.save_source(
        row,
        source_id="src_cursor",
        table_key="main",
        kind="external",
        mode="append",
        mapping={"name": "title", "count": "qty"},
        params={
            "instance_id": "conn1",
            "tool": "search",
            "args": {"q": "cup"},
            "result_path": "data.items",
            "cursor_arg": "since",
            "cursor_path": "data.next",
        },
        created_by=world.alice_id,
    )

    first = await service.run_source(row, "src_cursor")
    assert first.imported == 1
    assert calls[0] == {"q": "cup"}
    assert service.get_source(row, "src_cursor").params["cursor"] == "2026-11-02"

    await service.run_source(row, "src_cursor")
    assert calls[1] == {"q": "cup", "since": "2026-11-02"}


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


async def test_import_rows_replace_with_no_records_keeps_the_table(world: World) -> None:
    """An empty replace must not wipe the table.

    ``replace`` writes the whole table, so zero accepted rows means zero
    rows on disk. A connector can transiently return ``[]``; that must not
    destroy the user's data.
    """
    app_id = await _create(world)
    row = world.service.get(app_id)
    await world.service.append_row(row, {"title": "keep-me", "status": "new"})

    with pytest.raises(OctopError):
        await world.service.import_rows(row, [], mode="replace")

    _, rows, total = await world.service.read_rows(world.service.get(app_id))
    assert total == 1
    assert rows[0]["title"] == "keep-me"


async def test_import_rows_replace_all_invalid_keeps_the_table(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    await world.service.append_row(row, {"title": "keep-me", "status": "new"})

    with pytest.raises(OctopError):
        await world.service.import_rows(
            row, [{"title": "", "qty": "1", "status": "new"}], mode="replace"
        )

    _, rows, total = await world.service.read_rows(world.service.get(app_id))
    assert total == 1
    assert rows[0]["title"] == "keep-me"


async def test_import_dry_run_writes_nothing(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    await world.service.append_row(row, {"title": "A", "qty": 1, "status": "new"})

    result = await world.service.import_rows(
        row,
        [
            {"title": "B", "qty": "2", "status": "done"},
            {"title": "", "qty": "3", "status": "new"},  # invalid: missing title
        ],
        dry_run=True,
    )
    assert result.dry_run is True
    assert (result.imported, result.would_import, result.skipped) == (0, 1, 1)
    assert len(result.errors) == 1

    # Nothing was written: the table still holds only the original row.
    _, rows, total = await world.service.read_rows(world.service.get(app_id))
    assert total == 1
    assert rows[0]["title"] == "A"


async def test_import_dry_run_replace_leaves_the_table(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    await world.service.append_row(row, {"title": "A", "status": "new"})

    result = await world.service.import_rows(
        row,
        [{"title": "X", "status": "done"}],
        mode="replace",
        dry_run=True,
    )
    assert result.dry_run is True
    assert result.would_import == 1

    _, rows, total = await world.service.read_rows(world.service.get(app_id))
    assert total == 1
    assert rows[0]["title"] == "A"


async def test_import_dry_run_reports_a_refused_replace(world: World) -> None:
    """A replace that would wipe the table is refused in a dry run too."""
    app_id = await _create(world)
    row = world.service.get(app_id)
    with pytest.raises(OctopError) as exc:
        await world.service.import_rows(
            row,
            [{"title": "", "status": "new"}],
            mode="replace",
            dry_run=True,
        )
    _assert_code(exc, ErrorCode.APP_ROW_INVALID)
    _, _rows, total = await world.service.read_rows(world.service.get(app_id))
    assert total == 0


async def test_import_from_workspace_file_with_header_row(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    await world.workspace.awrite_text(
        "inbound/exports/ledger.csv",
        "2026 年 9 月工资表\ntitle,qty,status\ncup,3,new\n",
    )

    # Without the header hint the title line is taken as the header, so the
    # data rows no longer match the schema and every row is rejected.
    naive = await world.service.import_from_file(row, path="inbound/exports/ledger.csv")
    assert (naive.imported, naive.skipped) == (0, 2)

    result = await world.service.import_from_file(
        row,
        path="inbound/exports/ledger.csv",
        header_row=1,
    )
    assert result.imported == 1
    _, rows, _total = await world.service.read_rows(world.service.get(app_id))
    assert rows[-1]["title"] == "cup"


async def test_import_from_workspace_file_with_sheet(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    await world.workspace.aupload_bytes(
        "inbound/exports/q3.xlsx",
        _xlsx_bytes(
            {"9月": [["title", "qty"], ["sep", "1"]], "10月": [["title", "qty"], ["oct", "2"]]}
        ),
    )

    result = await world.service.import_from_file(
        row,
        path="inbound/exports/q3.xlsx",
        sheet="10月",
    )
    assert result.imported == 1
    _, rows, _total = await world.service.read_rows(world.service.get(app_id))
    assert rows[0]["title"] == "oct"

    with pytest.raises(OctopError) as exc:
        await world.service.import_from_file(
            row,
            path="inbound/exports/q3.xlsx",
            sheet="nope",
        )
    _assert_code(exc, ErrorCode.APP_ROW_INVALID)


async def test_transfer_moves_the_app_to_another_agent(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    await world.service.write_html(row, "<h1>Hello</h1>")
    await world.service.append_row(row, {"title": "A", "status": "new"})
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 16
    await world.service.save_asset(row, "cover.png", png)

    moved = await world.service.transfer(
        row,
        target_agent_id=world.other_agent_id,
        actor_user_id=world.alice_id,
    )
    assert moved.agent_id == world.other_agent_id
    assert world.repo.get(app_id).agent_id == world.other_agent_id

    # The files, assets and rows moved with the app.
    assert await world.other_workspace.aread_text(f"apps/{app_id}/app.html") == "<h1>Hello</h1>"
    assert (await world.service.read_asset(world.service.get(app_id), "cover.png"))[1] == png
    _, rows, total = await world.service.read_rows(world.service.get(app_id))
    assert total == 1
    assert rows[0]["title"] == "A"

    # The source copy stays until the source agent is deleted.
    assert await world.workspace.aread_text(f"apps/{app_id}/app.html") == "<h1>Hello</h1>"


async def test_transfer_rejects_an_unreachable_target(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)

    with pytest.raises(OctopError) as exc:
        await world.service.transfer(
            row,
            target_agent_id=world.agent_id,
            actor_user_id=world.alice_id,
        )
    _assert_code(exc, ErrorCode.APP_SOURCE_INVALID)

    with pytest.raises(OctopError) as exc:
        await world.service.transfer(
            row,
            target_agent_id="missing_agent",
            actor_user_id=world.alice_id,
        )
    _assert_code(exc, ErrorCode.APP_SOURCE_INVALID)


async def test_asset_round_trip(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32

    assert await world.service.save_asset(row, "cover.png", png) == "cover.png"
    mime, data = await world.service.read_asset(row, "cover.png")
    assert (mime, data) == ("image/png", png)
    assert await world.service.list_assets(row) == ["cover.png"]


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("../escape.png", b"\x89PNG\r\n\x1a\n"),
        (".hidden.png", b"\x89PNG\r\n\x1a\n"),
        ("cover.svg", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"),
        ("cover.png", b"<html>not an image</html>"),
        ("cover.png", b""),
    ],
)
async def test_asset_rejects_bad_input(world: World, name: str, data: bytes) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    with pytest.raises(OctopError) as exc:
        await world.service.save_asset(row, name, data)
    _assert_code(exc, ErrorCode.APP_SOURCE_INVALID)


async def test_asset_rejects_oversized_files(world: World) -> None:
    from octop.infra.apps.service import MAX_ASSET_BYTES

    app_id = await _create(world)
    row = world.service.get(app_id)
    too_big = b"\x89PNG\r\n\x1a\n" + b"0" * MAX_ASSET_BYTES
    with pytest.raises(OctopError) as exc:
        await world.service.save_asset(row, "big.png", too_big)
    _assert_code(exc, ErrorCode.APP_SOURCE_INVALID)


async def test_asset_accepts_webp_and_gif(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    webp = b"RIFF\x00\x00\x00\x00WEBP" + b"0" * 16
    gif = b"GIF89a" + b"0" * 16

    await world.service.save_asset(row, "a.webp", webp)
    await world.service.save_asset(row, "b.gif", gif)
    assert (await world.service.read_asset(row, "a.webp"))[0] == "image/webp"
    assert (await world.service.read_asset(row, "b.gif"))[0] == "image/gif"


async def test_append_is_idempotent_with_client_request_id(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)

    first_index, first = await world.service.append_row_record(
        row,
        {"title": "A", "status": "new"},
        client_request_id="req-1",
    )
    second_index, second = await world.service.append_row_record(
        row,
        {"title": "A", "status": "new"},
        client_request_id="req-1",
    )
    assert second.row_id == first.row_id
    assert second_index == first_index
    assert world.repo.count_data_rows(app_id, "main") == 1


async def test_source_key_upserts_instead_of_duplicating(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)

    _, first = await world.service.append_row_record(
        row,
        {"title": "A", "status": "new"},
        source="connector",
        source_id="src_1",
        source_key="remote-1",
    )
    _, second = await world.service.append_row_record(
        row,
        {"title": "A updated", "status": "done"},
        source="connector",
        source_id="src_1",
        source_key="remote-1",
    )
    assert second.row_id == first.row_id
    assert second.version == 2
    assert world.repo.count_data_rows(app_id, "main") == 1
    _, rows, _total = await world.service.read_rows(row)
    assert rows[0]["title"] == "A updated"


async def test_update_and_delete_row_with_cas(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    _, stored = await world.service.append_row_record(row, {"title": "A", "status": "new"})

    updated = await world.service.update_row(
        row,
        stored.row_id,
        {"title": "B", "status": "done"},
        expected_version=1,
    )
    assert updated["version"] == 2
    assert updated["values"] == {"title": "B", "qty": "", "status": "done"}

    with pytest.raises(OctopError) as exc:
        await world.service.update_row(
            row,
            stored.row_id,
            {"title": "C", "status": "new"},
            expected_version=1,
        )
    _assert_code(exc, ErrorCode.APP_CONFLICT)

    with pytest.raises(OctopError) as exc:
        await world.service.update_row(row, "row_missing", {"title": "C"})
    _assert_code(exc, ErrorCode.APP_ROW_NOT_FOUND)

    assert await world.service.delete_row(row, stored.row_id, expected_version=2) == stored.row_id
    _, _rows, total = await world.service.read_rows(row)
    assert total == 0


async def test_legacy_csv_app_migrates_on_first_read(world: World) -> None:
    app_id = await _create(world)
    _make_legacy(world, app_id)
    await world.workspace.awrite_text(
        app_path(app_id, "data.csv"),
        "title,qty,status\nA,1,new\nB,,done\n",
    )

    schema, rows, total = await world.service.read_rows(world.service.get(app_id))
    assert schema.names == ("title", "qty", "status")
    assert total == 2
    assert [row["title"] for row in rows] == ["A", "B"]
    migrated = world.repo.get(app_id)
    assert migrated.storage_version == 2
    assert world.repo.count_data_rows(app_id, "main") == 2

    # Running the lazy migration again must not duplicate rows.
    _, _rows, total = await world.service.read_rows(world.service.get(app_id))
    assert total == 2


async def test_export_rows_renders_csv_from_db(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    await world.service.append_row(row, {"title": "A", "qty": 2, "status": "new"})

    filename, content = await world.service.export_rows(row)
    assert filename == "main.csv"
    assert content == "title,qty,status\nA,2,new\n"


async def test_alter_schema_renames_and_drops_columns(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    _, stored = await world.service.append_row_record(
        row,
        {"title": "A", "qty": 2, "status": "new"},
    )

    schema = await world.service.alter_schema(
        row,
        renames={"title": "name"},
        drops=["qty"],
    )
    assert schema.version == 2
    assert schema.names == ("name", "status")
    _, rows, _meta, total = await world.service.read_rows_detailed(world.service.get(app_id))
    assert total == 1
    assert rows[0] == {"name": "A", "status": "new"}
    assert world.repo.get_data_row(app_id, stored.row_id) is not None


async def test_alter_schema_rejects_rows_that_do_not_fit(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    # Store a title that cannot become an integer. The row is written through
    # the repo because the service would reject it against the current schema.
    _, stored = await world.service.append_row_record(row, {"title": "A", "status": "new"})
    world.repo.update_data_row(
        app_id,
        stored.row_id,
        {"title": "not-a-number", "qty": "", "status": "new"},
    )

    with pytest.raises(OctopError) as exc:
        await world.service.alter_schema(row, types={"title": {"type": "integer"}})
    _assert_code(exc, ErrorCode.APP_ROW_INVALID)
    # The schema is unchanged after the rejected migration.
    assert (await world.service.read_schema(row)).column("title").type == "string"


async def test_alter_schema_retypes_and_validates(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    await world.service.append_row(row, {"title": "42", "status": "new"})

    schema = await world.service.alter_schema(row, types={"title": {"type": "integer"}})
    assert schema.column("title").type == "integer"
    _, rows, _meta, _total = await world.service.read_rows_detailed(row)
    assert rows[0]["title"] == "42"


async def test_import_upsert_key_updates_instead_of_duplicating(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)

    first = await world.service.import_rows(
        row,
        [
            {"title": "A", "qty": "1", "status": "new"},
            {"title": "B", "qty": "2", "status": "new"},
        ],
        upsert_key=["title"],
    )
    assert (first.imported, first.updated) == (2, 0)

    second = await world.service.import_rows(
        row,
        [
            {"title": "A", "qty": "9", "status": "done"},
            {"title": "C", "qty": "3", "status": "new"},
        ],
        upsert_key=["title"],
    )
    assert (second.imported, second.updated) == (1, 1)

    _, rows, _meta, total = await world.service.read_rows_detailed(world.service.get(app_id))
    assert total == 3
    by_title = {item["title"]: item for item in rows}
    assert by_title["A"]["qty"] == "9"
    assert by_title["A"]["status"] == "done"
    assert by_title["C"]["qty"] == "3"


async def test_import_rejects_unknown_upsert_columns(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    with pytest.raises(OctopError) as exc:
        await world.service.import_rows(
            row,
            [{"title": "A"}],
            upsert_key=["nope"],
        )
    _assert_code(exc, ErrorCode.APP_SCHEMA_INVALID)


async def test_import_from_several_files_merges_rows(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    await world.workspace.awrite_text("inbound/sep.csv", "title,qty,status\nA,1,new\n")
    await world.workspace.awrite_text("inbound/oct.csv", "title,qty,status\nB,2,done\n")

    result = await world.service.import_from_files(
        row,
        paths=["inbound/sep.csv", "inbound/oct.csv"],
    )
    assert result.imported == 2
    _, rows, _meta, total = await world.service.read_rows_detailed(row)
    assert total == 2
    assert [item["title"] for item in rows] == ["A", "B"]


async def test_import_from_several_files_rejects_missing_file(world: World) -> None:
    app_id = await _create(world)
    row = world.service.get(app_id)
    await world.workspace.awrite_text("inbound/sep.csv", "title\nA\n")
    with pytest.raises(OctopError) as exc:
        await world.service.import_from_files(
            row,
            paths=["inbound/sep.csv", "inbound/missing.csv"],
        )
    _assert_code(exc, ErrorCode.APP_ROW_INVALID)


async def test_import_reports_pii_warnings(world: World) -> None:
    row = await world.service.create(
        agent_id=world.agent_id,
        owner_user_id=world.alice_id,
        name="Payroll",
        columns=[
            {"name": "name", "type": "text"},
            {"name": "id_card", "type": "text"},
        ],
    )
    result = await world.service.import_rows(
        row,
        [{"name": "张悦", "id_card": "330101199001011234"}],
    )
    assert result.imported == 1
    assert any("id_card" in warning for warning in result.warnings)


async def test_app_templates_list_and_create(world: World) -> None:
    schema = json.dumps(
        {
            "version": 1,
            "columns": [
                {"name": "title", "label": "书名", "type": "text", "required": True},
                {"name": "status", "label": "状态", "type": "enum", "values": ["想读", "读完"]},
            ],
        },
        ensure_ascii=False,
    )
    html = "<!doctype html><html><body><h1>Books</h1><script>octopApp.listRows();</script></body></html>"
    await world.workspace.awrite_text("app_templates/reading/schema.json", schema)
    await world.workspace.awrite_text("app_templates/reading/app.html", html)

    templates = await world.service.list_app_templates(world.agent_id)
    assert templates == [{"template": "reading", "label": "reading", "has_html": True}]

    row = await world.service.create_from_template(
        agent_id=world.agent_id,
        owner_user_id=world.alice_id,
        template="reading",
        name="我的读书清单",
    )
    assert row.row_count == 0
    assert row.storage_version == 2
    schema_now = await world.service.read_schema(row)
    assert schema_now.names == ("title", "status")
    assert await world.service.read_html(row) == html

    with pytest.raises(OctopError) as exc:
        await world.service.create_from_template(
            agent_id=world.agent_id,
            owner_user_id=world.alice_id,
            template="../secret",
        )
    _assert_code(exc, ErrorCode.APP_NOT_FOUND)


async def test_backup_and_restore_round_trip(world: World) -> None:
    app_id = await _create(world, name="读书清单")
    row = world.service.get(app_id)
    await world.service.append_row(row, {"title": "A", "qty": "2", "status": "new"})
    await world.service.create_table(
        row,
        table_key="notes",
        label="笔记",
        columns=[{"name": "note", "type": "string"}],
    )
    await world.service.append_row(row, {"note": "hello"}, table="notes")
    html = "<!doctype html><html><body><h1>Books</h1><script>octopApp.listRows();</script></body></html>"
    await world.service.write_html(row, html)
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    await world.service.save_asset(row, "cover.png", png)

    filename, archive = await world.service.backup_archive(world.service.get(app_id))
    assert filename == f"light-app-{app_id}.zip"
    assert archive.startswith(b"PK")

    restored = await world.service.restore_archive(
        agent_id=world.other_agent_id,
        owner_user_id=world.bob_id,
        data=archive,
    )
    assert restored.app_id != app_id
    assert restored.agent_id == world.other_agent_id
    assert restored.owner_user_id == world.bob_id
    assert restored.name == "读书清单"
    assert restored.description == "things"
    assert restored.storage_version == 2

    schema, rows, total = await world.service.read_rows(restored)
    assert total == 1
    assert schema.names == ("title", "qty", "status")
    assert rows == [{"title": "A", "qty": "2", "status": "new"}]

    notes_schema, notes_rows, notes_total = await world.service.read_rows(restored, table="notes")
    assert notes_schema.names == ("note",)
    assert notes_total == 1
    assert notes_rows == [{"note": "hello"}]

    assert await world.service.read_html(restored) == html
    assert await world.service.list_assets(restored) == ["cover.png"]
    assert (await world.service.read_asset(restored, "cover.png"))[1] == png

    # The source app is untouched.
    _schema, source_rows, source_total = await world.service.read_rows(world.service.get(app_id))
    assert source_total == 1
    assert source_rows == [{"title": "A", "qty": "2", "status": "new"}]


async def test_restore_rejects_files_that_are_not_backups(world: World) -> None:
    with pytest.raises(OctopError) as exc:
        await world.service.restore_archive(
            agent_id=world.agent_id,
            owner_user_id=world.alice_id,
            data=b"not a zip",
        )
    _assert_code(exc, ErrorCode.APP_SOURCE_INVALID)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"format": "something-else"}))
    with pytest.raises(OctopError) as exc:
        await world.service.restore_archive(
            agent_id=world.agent_id,
            owner_user_id=world.alice_id,
            data=buffer.getvalue(),
        )
    _assert_code(exc, ErrorCode.APP_SOURCE_INVALID)
    assert world.repo.list_owned(world.alice_id) == []


async def test_import_blocks_sensitive_columns_by_default(world: World) -> None:
    row = await world.service.create(
        agent_id=world.agent_id,
        owner_user_id=world.alice_id,
        name="Contacts",
        columns=[
            {"name": "title", "type": "string"},
            {"name": "phone", "type": "string"},
        ],
    )

    result = await world.service.import_rows(
        row,
        [{"title": "A", "phone": "13800138000"}],
    )

    assert result.imported == 1
    assert result.pii_columns == ["phone"]
    assert result.blocked_pii == ["phone"]
    assert result.failed_rows == []
    assert any("include_pii=true" in warning for warning in result.warnings)

    schema = await world.service.read_schema(row)
    assert schema.column("phone") is not None
    assert schema.column("phone").pii == "phone"
    _schema, rows, _total = await world.service.read_rows(world.service.get(row.app_id))
    assert rows == [{"title": "A", "phone": ""}]

    _schema, masked, _total = await world.service.read_rows(
        world.service.get(row.app_id), redact_pii=True
    )
    assert masked == [{"title": "A", "phone": "••••"}]

    _filename, exported = await world.service.export_rows(
        world.service.get(row.app_id), redact_pii=True
    )
    assert "••••" in exported
    assert "13800138000" not in exported


async def test_import_include_pii_keeps_sensitive_values(world: World) -> None:
    row = await world.service.create(
        agent_id=world.agent_id,
        owner_user_id=world.alice_id,
        name="Contacts",
        columns=[
            {"name": "title", "type": "string"},
            {"name": "phone", "type": "string", "pii": "phone"},
        ],
    )

    result = await world.service.import_rows(
        row,
        [{"title": "A", "phone": "13800138000"}],
        include_pii=True,
    )

    assert result.imported == 1
    assert result.pii_columns == ["phone"]
    assert result.blocked_pii == []
    _schema, rows, _total = await world.service.read_rows(world.service.get(row.app_id))
    assert rows == [{"title": "A", "phone": "13800138000"}]


async def test_import_rejects_a_required_sensitive_column_by_default(world: World) -> None:
    row = await world.service.create(
        agent_id=world.agent_id,
        owner_user_id=world.alice_id,
        name="Contacts",
        columns=[
            {"name": "title", "type": "string"},
            {"name": "phone", "type": "string", "required": True, "pii": "phone"},
        ],
    )

    with pytest.raises(OctopError) as exc:
        await world.service.import_rows(
            row,
            [{"title": "A", "phone": "13800138000"}],
        )
    _assert_code(exc, ErrorCode.APP_ROW_INVALID)
    assert "include_pii=true" in exc.value.message
    _schema, rows, _total = await world.service.read_rows(world.service.get(row.app_id))
    assert rows == []
