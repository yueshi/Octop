"""Unit tests for the built-in light-app agent tools."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from deepagents.backends import FilesystemBackend
from langgraph.config import var_child_runnable_config
from octop_harness.backends import BackendWorkspace

from octop.infra.agents.tool_scope import tool_scope
from octop.infra.apps.service import AppService
from octop.infra.apps.tools import APP_DELETE_TOOL_NAME, APP_TOOL_NAMES, build_app_tools
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.apps import LightAppRepo
from octop.infra.db.repos.secrets import SecretRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.utils.ulid import new_ulid

COLUMNS: list[dict[str, object]] = [
    {"name": "title", "type": "string", "required": True},
    {"name": "status", "type": "enum", "values": ["new", "done"]},
]


@contextmanager
def _configurable(**kwargs: object):
    token = var_child_runnable_config.set({"configurable": kwargs})
    try:
        yield
    finally:
        var_child_runnable_config.reset(token)


@dataclass
class World:
    service: AppService
    agent_repo: AgentRepo
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
    return World(
        service=AppService(
            app_repo=LightAppRepo(db),
            agent_repo=agent_repo,
            user_repo=user_repo,
            workspaces=_Workspaces(agent_id, workspace),
            secret_repo=SecretRepo(db),
        ),
        agent_repo=agent_repo,
        agent_id=agent_id,
        alice_id=alice_id,
        bob_id=bob_id,
    )


def _tool_by_name(tools: list, name: str):
    for tool in tools:
        if tool.name == name:
            return tool
    raise KeyError(name)


def test_build_app_tools_names(world: World) -> None:
    names = {tool.name for tool in build_app_tools(world.service)}
    assert names == {
        "app_inspect_file",
        "app_list",
        "app_open",
        "app_share_link",
        "app_create",
        "app_create_from_template",
        "app_add_table",
        "app_update_schema",
        "app_alter_schema",
        "app_set_table_visibility",
        "app_write_html",
        "app_read_data",
        "app_append_row",
        "app_update_row",
        "app_delete_row",
        "app_import_rows",
        "app_save_asset",
        "app_set_preview",
        APP_DELETE_TOOL_NAME,
    }
    # The manager decides whether to inject the light-app prompt from this
    # list, so it must stay in sync with the registered tools.
    assert tuple(tool.name for tool in build_app_tools(world.service)) == APP_TOOL_NAMES


async def test_tool_lifecycle(world: World) -> None:
    tools = build_app_tools(world.service)
    list_tool = _tool_by_name(tools, "app_list")
    create = _tool_by_name(tools, "app_create")
    update_schema = _tool_by_name(tools, "app_update_schema")
    write_html = _tool_by_name(tools, "app_write_html")
    read_data = _tool_by_name(tools, "app_read_data")
    append = _tool_by_name(tools, "app_append_row")
    delete = _tool_by_name(tools, APP_DELETE_TOOL_NAME)

    with _configurable(agent_id=world.agent_id, user=str(world.alice_id)):
        created = json.loads(
            await create.ainvoke({"name": "Tracker", "description": "things", "columns": COLUMNS})
        )
        app_id = created["app_id"]
        assert created["name"] == "Tracker"
        assert created["row_count"] == 0

        listed = json.loads(await list_tool.ainvoke({}))
        assert [item["app_id"] for item in listed["apps"]] == [app_id]

        extended = json.loads(
            await update_schema.ainvoke(
                {"app_id": app_id, "columns": [{"name": "notes", "type": "text"}]}
            )
        )
        assert extended["version"] == 2
        assert [col["name"] for col in extended["columns"]] == ["title", "status", "notes"]

        written = json.loads(await write_html.ainvoke({"app_id": app_id, "html": "<h1>x</h1>"}))
        assert written["bytes"] == len("<h1>x</h1>")

        appended = json.loads(
            await append.ainvoke({"app_id": app_id, "row": {"title": "A", "status": "new"}})
        )
        assert appended["row_index"] == 0
        assert appended["row_id"].startswith("row_")
        assert "row" not in appended

        read = json.loads(await read_data.ainvoke({"app_id": app_id, "limit": 10}))
        assert read["total"] == 1
        assert read["rows"] == [{"title": "A", "status": "new", "notes": ""}]
        assert [col["name"] for col in read["columns"]] == ["title", "status", "notes"]

        rejected = json.loads(await append.ainvoke({"app_id": app_id, "row": {"nope": "1"}}))
        assert "error" in rejected

        deleted = json.loads(await delete.ainvoke({"app_id": app_id}))
        assert deleted == {"deleted": app_id}
        assert json.loads(await list_tool.ainvoke({})) == {"apps": []}


async def test_tool_write_html_rejects_external_resources(world: World) -> None:
    tools = build_app_tools(world.service)
    create = _tool_by_name(tools, "app_create")
    write_html = _tool_by_name(tools, "app_write_html")

    with _configurable(agent_id=world.agent_id, user=str(world.alice_id)):
        app_id = json.loads(await create.ainvoke({"name": "Tracker", "columns": COLUMNS}))["app_id"]

        rejected = json.loads(
            await write_html.ainvoke(
                {"app_id": app_id, "html": '<script src="https://cdn.example.com/x.js"></script>'}
            )
        )
        assert "error" in rejected
        # The agent has to be able to fix the page from this message alone.
        assert "external script" in rejected["error"]
        assert "inline all CSS and JS" in rejected["error"]
        assert "octopApp.openExternal" in rejected["error"]

        written = json.loads(
            await write_html.ainvoke(
                {"app_id": app_id, "html": '<h1>ok</h1><img src="data:image/png;base64,AA">'}
            )
        )
        assert written["bytes"] > 0


async def test_multi_table_tools(world: World) -> None:
    tools = build_app_tools(world.service)
    create = _tool_by_name(tools, "app_create")
    add_table = _tool_by_name(tools, "app_add_table")
    append = _tool_by_name(tools, "app_append_row")
    read_data = _tool_by_name(tools, "app_read_data")
    list_tool = _tool_by_name(tools, "app_list")

    with _configurable(agent_id=world.agent_id, user=str(world.alice_id)):
        created = json.loads(await create.ainvoke({"name": "Shop", "columns": COLUMNS}))
        app_id = created["app_id"]

        added = json.loads(
            await add_table.ainvoke(
                {
                    "app_id": app_id,
                    "key": "orders",
                    "label": "Orders",
                    "columns": [{"name": "item", "type": "string", "required": True}],
                }
            )
        )
        assert added["table"] == "orders"
        assert added["label"] == "Orders"

        duplicate = json.loads(
            await add_table.ainvoke({"app_id": app_id, "key": "orders", "columns": [{"name": "x"}]})
        )
        assert "already exists" in duplicate["error"]

        rejected = json.loads(
            await add_table.ainvoke({"app_id": app_id, "key": "main", "columns": [{"name": "x"}]})
        )
        assert "already exists" in rejected["error"]

        appended = json.loads(
            await append.ainvoke({"app_id": app_id, "row": {"item": "cup"}, "table": "orders"})
        )
        assert appended["table"] == "orders"
        assert appended["row_id"].startswith("row_")
        assert "row" not in appended

        read = json.loads(await read_data.ainvoke({"app_id": app_id, "table": "orders"}))
        assert read["total"] == 1
        assert read["rows"] == [{"item": "cup"}]

        main = json.loads(await read_data.ainvoke({"app_id": app_id}))
        assert main["table"] == "main"
        assert main["total"] == 0

        listed = json.loads(await list_tool.ainvoke({}))
        tables = listed["apps"][0]["tables"]
        assert [item["table_key"] for item in tables] == ["main", "orders"]
        assert tables[1]["row_count"] == 1

        missing = json.loads(await read_data.ainvoke({"app_id": app_id, "table": "nope"}))
        assert "unknown table" in missing["error"]


async def test_tools_reject_non_owner(world: World) -> None:
    tools = build_app_tools(world.service)
    create = _tool_by_name(tools, "app_create")
    list_tool = _tool_by_name(tools, "app_list")
    read_data = _tool_by_name(tools, "app_read_data")

    with _configurable(agent_id=world.agent_id, user=str(world.alice_id)):
        created = json.loads(await create.ainvoke({"name": "Tracker", "columns": COLUMNS}))
        app_id = created["app_id"]

    with _configurable(agent_id=world.agent_id, user=str(world.bob_id)):
        denied = json.loads(await create.ainvoke({"name": "Theirs", "columns": COLUMNS}))
        assert "cannot use this agent" in denied["error"]
        assert json.loads(await list_tool.ainvoke({})) == {"apps": []}
        hidden = json.loads(await read_data.ainvoke({"app_id": app_id}))
        assert "not accessible" in hidden["error"]


async def test_tools_follow_the_app_acl_not_the_owning_agent(world: World) -> None:
    """The app ACL decides what the user may do; the agent is only the executor."""
    tools = build_app_tools(world.service)
    create = _tool_by_name(tools, "app_create")
    read_data = _tool_by_name(tools, "app_read_data")

    with _configurable(agent_id=world.agent_id, user=str(world.alice_id)):
        created = json.loads(await create.ainvoke({"name": "Tracker", "columns": COLUMNS}))
        app_id = created["app_id"]

    other_agent = new_ulid()
    world.agent_repo.create(agent_id=other_agent, user_id=world.alice_id, name="other")
    with _configurable(agent_id=other_agent, user=str(world.alice_id)):
        out = json.loads(await read_data.ainvoke({"app_id": app_id}))
    assert out["total"] == 0


async def test_tools_require_agent_and_user_context(world: World) -> None:
    tools = build_app_tools(world.service)
    list_tool = _tool_by_name(tools, "app_list")
    with _configurable():
        out = json.loads(await list_tool.ainvoke({}))
    assert "configurable.agent_id" in out["error"]


async def test_tools_use_resume_scope_without_configurable(world: World) -> None:
    """HITL resume invocations carry no configurable; the acting user comes from scope."""
    tools = build_app_tools(world.service)
    create = _tool_by_name(tools, "app_create")
    delete = _tool_by_name(tools, APP_DELETE_TOOL_NAME)

    with tool_scope(world.agent_id, world.alice_id):
        created = json.loads(await create.ainvoke({"name": "Tracker", "columns": COLUMNS}))
        assert created["app_id"]
        deleted = json.loads(await delete.ainvoke({"app_id": created["app_id"]}))
        assert deleted == {"deleted": created["app_id"]}

    # The scope must not leak past the resume invocation.
    with _configurable():
        out = json.loads(await create.ainvoke({"name": "Again", "columns": COLUMNS}))
    assert "configurable.agent_id" in out["error"]


async def test_tool_inspect_file_reports_sheets(world: World) -> None:
    tools = build_app_tools(world.service)
    inspect = _tool_by_name(tools, "app_inspect_file")

    workspace = world.service._workspaces.workspace_for_agent(world.agent_id)
    assert workspace is not None
    await workspace.aupload_bytes(
        "inbound/items.csv",
        b"name,qty\ncup,3\npen,4\n",
    )

    with _configurable(agent_id=world.agent_id, user=str(world.alice_id)):
        envelope = json.loads(await inspect.ainvoke({"path": "inbound/items.csv"}))
        assert envelope["octop_ui"]["renderer"] == "app_inspect_card"
        report = envelope["data"]

    assert report["file"] == "inbound/items.csv"
    sheet = report["sheets"][0]
    assert sheet["header"] == ["name", "qty"]
    assert sheet["rows"] == 2
    assert [column["type"] for column in sheet["columns"]] == ["string", "integer"]


async def test_tool_inspect_file_rejects_missing_and_hidden_paths(world: World) -> None:
    tools = build_app_tools(world.service)
    inspect = _tool_by_name(tools, "app_inspect_file")

    with _configurable(agent_id=world.agent_id, user=str(world.alice_id)):
        missing = json.loads(await inspect.ainvoke({"path": "inbound/nope.csv"}))
        assert "not found" in missing["error"]

        hidden = json.loads(await inspect.ainvoke({"path": ".octop/secret.csv"}))
        assert "error" in hidden


async def test_tool_inspect_file_denies_other_users(world: World) -> None:
    tools = build_app_tools(world.service)
    inspect = _tool_by_name(tools, "app_inspect_file")

    workspace = world.service._workspaces.workspace_for_agent(world.agent_id)
    assert workspace is not None
    await workspace.aupload_bytes("inbound/items.csv", b"name\ncup\n")

    with _configurable(agent_id=world.agent_id, user=str(world.bob_id)):
        denied = json.loads(await inspect.ainvoke({"path": "inbound/items.csv"}))
    assert "error" in denied


async def test_tool_import_rows_dry_run_writes_nothing(world: World) -> None:
    tools = build_app_tools(world.service)
    create = _tool_by_name(tools, "app_create")
    import_rows = _tool_by_name(tools, "app_import_rows")

    workspace = world.service._workspaces.workspace_for_agent(world.agent_id)
    assert workspace is not None
    await workspace.awrite_text("inbound/items.csv", "title,qty\ncup,3\npen,4\n")

    with _configurable(agent_id=world.agent_id, user=str(world.alice_id)):
        app_id = json.loads(await create.ainvoke({"name": "Tracker", "columns": COLUMNS}))["app_id"]

        preview = json.loads(
            await import_rows.ainvoke(
                {"app_id": app_id, "path": "inbound/items.csv", "dry_run": True}
            )
        )
        assert preview["dry_run"] is True
        assert preview["would_import"] == 2
        assert preview["imported"] == 0

        read = json.loads(await _tool_by_name(tools, "app_read_data").ainvoke({"app_id": app_id}))
        assert read["total"] == 0

        real = json.loads(
            await import_rows.ainvoke({"app_id": app_id, "path": "inbound/items.csv"})
        )
        assert real["imported"] == 2
        assert real["dry_run"] is False


async def test_shared_agent_members_can_create_and_use_their_own_apps(world: World) -> None:
    """A shared agent's members get the app tools; the app belongs to its creator."""
    world.agent_repo.set_shared(world.agent_id, True)
    tools = build_app_tools(world.service)
    create = _tool_by_name(tools, "app_create")
    list_tool = _tool_by_name(tools, "app_list")
    append = _tool_by_name(tools, "app_append_row")
    read_data = _tool_by_name(tools, "app_read_data")

    with _configurable(agent_id=world.agent_id, user=str(world.bob_id)):
        created = json.loads(await create.ainvoke({"name": "Bob's", "columns": COLUMNS}))
        app_id = created["app_id"]
        assert created["name"] == "Bob's"
        # The app is listed for its creator, not for the agent owner.
        assert [item["app_id"] for item in json.loads(await list_tool.ainvoke({}))["apps"]] == [
            app_id
        ]
        appended = json.loads(
            await append.ainvoke({"app_id": app_id, "row": {"title": "A", "status": "new"}})
        )
        assert appended["row_id"].startswith("row_")

    # The agent owner neither sees nor touches an app another member created.
    with _configurable(agent_id=world.agent_id, user=str(world.alice_id)):
        assert json.loads(await list_tool.ainvoke({})) == {"apps": []}
        denied = json.loads(await read_data.ainvoke({"app_id": app_id}))
    assert "no 'view' permission" in denied["error"]


async def test_collaborator_can_append_but_not_change_structure(world: World) -> None:
    world.agent_repo.set_shared(world.agent_id, True)
    tools = build_app_tools(world.service)
    create = _tool_by_name(tools, "app_create")
    append = _tool_by_name(tools, "app_append_row")
    write_html = _tool_by_name(tools, "app_write_html")

    with _configurable(agent_id=world.agent_id, user=str(world.alice_id)):
        app_id = json.loads(await create.ainvoke({"name": "Tracker", "columns": COLUMNS}))["app_id"]

    world.service.set_shares(world.service.get(app_id), [("bob", "edit_data")])

    with _configurable(agent_id=world.agent_id, user=str(world.bob_id)):
        appended = json.loads(
            await append.ainvoke({"app_id": app_id, "row": {"title": "B", "status": "new"}})
        )
        assert appended["row_id"].startswith("row_")

        denied = json.loads(await write_html.ainvoke({"app_id": app_id, "html": "<h1>x</h1>"}))
    assert "no 'edit_structure' permission" in denied["error"]


async def test_tool_save_asset_from_workspace_file(world: World) -> None:
    tools = build_app_tools(world.service)
    create = _tool_by_name(tools, "app_create")
    save_asset = _tool_by_name(tools, "app_save_asset")

    workspace = world.service._workspaces.workspace_for_agent(world.agent_id)
    assert workspace is not None
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 16
    await workspace.aupload_bytes("inbound/cover.png", png)

    with _configurable(agent_id=world.agent_id, user=str(world.alice_id)):
        app_id = json.loads(await create.ainvoke({"name": "Tracker", "columns": COLUMNS}))["app_id"]
        out = json.loads(
            await save_asset.ainvoke(
                {"app_id": app_id, "source": "inbound/cover.png", "name": "cover.png"}
            )
        )
    assert out["name"] == "cover.png"
    assert out["size"] == len(png)
    assert out["usage"] == 'octopApp.asset("cover.png")'


async def test_tool_save_asset_rejects_hidden_paths(world: World) -> None:
    tools = build_app_tools(world.service)
    create = _tool_by_name(tools, "app_create")
    save_asset = _tool_by_name(tools, "app_save_asset")

    with _configurable(agent_id=world.agent_id, user=str(world.alice_id)):
        app_id = json.loads(await create.ainvoke({"name": "Tracker", "columns": COLUMNS}))["app_id"]
        out = json.loads(
            await save_asset.ainvoke(
                {"app_id": app_id, "source": ".octop/auth/x.png", "name": "x.png"}
            )
        )
    assert "error" in out


async def test_tool_set_preview_stores_the_cover(world: World) -> None:
    tools = build_app_tools(world.service)
    create = _tool_by_name(tools, "app_create")
    set_preview = _tool_by_name(tools, "app_set_preview")

    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 630"></svg>'
    with _configurable(agent_id=world.agent_id, user=str(world.alice_id)):
        app_id = json.loads(await create.ainvoke({"name": "Tracker", "columns": COLUMNS}))["app_id"]
        out = json.loads(await set_preview.ainvoke({"app_id": app_id, "svg": svg}))
    assert out["app_id"] == app_id
    assert out["preview"] == "preview.svg"
    assert out["size"] == len(svg.encode("utf-8"))


async def test_tool_set_preview_rejects_active_content(world: World) -> None:
    tools = build_app_tools(world.service)
    create = _tool_by_name(tools, "app_create")
    set_preview = _tool_by_name(tools, "app_set_preview")

    with _configurable(agent_id=world.agent_id, user=str(world.alice_id)):
        app_id = json.loads(await create.ainvoke({"name": "Tracker", "columns": COLUMNS}))["app_id"]
        out = json.loads(
            await set_preview.ainvoke(
                {"app_id": app_id, "svg": '<svg xmlns="x"><script>alert(1)</script></svg>'}
            )
        )
    assert "error" in out


async def test_tool_create_app_from_template(world: World) -> None:
    tools = build_app_tools(world.service)
    create_from_template = _tool_by_name(tools, "app_create_from_template")

    workspace = world.service._workspaces.workspace_for_agent(world.agent_id)
    assert workspace is not None
    await workspace.awrite_text(
        "app_templates/reading/schema.json",
        json.dumps({"columns": [{"name": "title", "type": "text"}]}),
    )
    await workspace.awrite_text(
        "app_templates/reading/app.html",
        "<!doctype html><html><body><h1>Books</h1></body></html>",
    )

    with _configurable(agent_id=world.agent_id, user=str(world.alice_id)):
        out = json.loads(await create_from_template.ainvoke({"template": "reading"}))

    assert out["name"] == "reading"
    assert out["row_count"] == 0
    assert await world.service.has_html(world.service.get(out["app_id"])) is True


async def test_app_open_returns_a_chat_card(world: World) -> None:
    tools = build_app_tools(world.service)
    create = _tool_by_name(tools, "app_create")
    write_html = _tool_by_name(tools, "app_write_html")
    open_app = _tool_by_name(tools, "app_open")

    with _configurable(agent_id=world.agent_id, user=str(world.alice_id)):
        app_id = json.loads(await create.ainvoke({"name": "Card app", "columns": COLUMNS}))[
            "app_id"
        ]
        written = json.loads(
            await write_html.ainvoke(
                {"app_id": app_id, "html": "<!doctype html><html><body>ok</body></html>"}
            )
        )
        opened = json.loads(await open_app.ainvoke({"app_id": app_id}))

    assert "octop_ui" not in written
    assert written["app_id"] == app_id
    assert written["has_html"] is True
    assert opened["octop_ui"]["renderer"] == "app_card"
    assert opened["data"]["app_id"] == app_id
    assert opened["data"]["name"] == "Card app"
    assert opened["data"]["has_html"] is True


async def test_app_share_link_reuses_link_and_returns_channel_payload(world: World) -> None:
    tools = build_app_tools(world.service, public_base_url="http://example.test")
    create = _tool_by_name(tools, "app_create")
    share = _tool_by_name(tools, "app_share_link")

    with _configurable(agent_id=world.agent_id, user=str(world.alice_id)):
        created = json.loads(
            await create.ainvoke({"name": "Tracker", "description": "things", "columns": COLUMNS})
        )
        app_id = created["app_id"]
        row = world.service.get(app_id)
        await world.service.write_preview(
            row,
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 630"></svg>',
        )

        first = json.loads(await share.ainvoke({"app_id": app_id}))
        second = json.loads(await share.ainvoke({"name": "Tracker"}))
        rotated = json.loads(await share.ainvoke({"app_id": app_id, "rotate": True}))

    assert isinstance(first, list)
    assert first[0]["type"] == "text"
    assert "http://example.test/p/" in first[0]["text"]
    assert second[0]["text"] == first[0]["text"]
    assert rotated[0]["text"] != first[0]["text"]
    assert first[1]["type"] == "image"
    assert first[1]["im_safe"] is False


async def test_app_open_uses_channel_safe_payload_on_im(world: World) -> None:
    tools = build_app_tools(world.service, public_base_url="http://example.test")
    create = _tool_by_name(tools, "app_create")
    open_tool = _tool_by_name(tools, "app_open")

    with _configurable(agent_id=world.agent_id, user=str(world.alice_id)):
        created = json.loads(
            await create.ainvoke({"name": "Tracker", "description": "things", "columns": COLUMNS})
        )
        app_id = created["app_id"]

    with _configurable(
        agent_id=world.agent_id,
        user=str(world.alice_id),
        octop_source="weixin/01M5F",
    ):
        im_payload = json.loads(await open_tool.ainvoke({"app_id": app_id}))

    with _configurable(
        agent_id=world.agent_id,
        user=str(world.alice_id),
        octop_source="dashboard/octop-dashboard",
    ):
        dashboard_payload = json.loads(await open_tool.ainvoke({"app_id": app_id}))

    assert isinstance(im_payload, list)
    assert "/p/" in im_payload[0]["text"]
    assert isinstance(dashboard_payload, dict)
    assert dashboard_payload["octop_ui"]["renderer"] == "app_card"


async def test_app_set_table_visibility_and_share_warning(world: World) -> None:
    tools = build_app_tools(world.service, public_base_url="http://example.test")
    create = _tool_by_name(tools, "app_create")
    add_table = _tool_by_name(tools, "app_add_table")
    set_visibility = _tool_by_name(tools, "app_set_table_visibility")
    share = _tool_by_name(tools, "app_share_link")

    with _configurable(agent_id=world.agent_id, user=str(world.alice_id)):
        app_id = json.loads(await create.ainvoke({"name": "Tracker", "columns": COLUMNS}))["app_id"]
        added = json.loads(
            await add_table.ainvoke(
                {
                    "app_id": app_id,
                    "key": "orders",
                    "columns": [{"name": "item", "type": "string"}],
                }
            )
        )
        assert added["public_visible"] is False

        before = json.loads(await share.ainvoke({"app_id": app_id}))
        warning = next(
            block["text"] for block in before if "Private tables" in block.get("text", "")
        )
        assert "orders" in warning

        visible = json.loads(
            await set_visibility.ainvoke({"app_id": app_id, "table": "orders", "public": True})
        )
        assert visible["public_visible"] is True

        after = json.loads(await share.ainvoke({"app_id": app_id}))

    assert all("Private tables" not in block.get("text", "") for block in after)
