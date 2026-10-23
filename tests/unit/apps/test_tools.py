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
from octop.infra.apps.tools import APP_DELETE_TOOL_NAME, build_app_tools
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.apps import LightAppRepo
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
        "app_list",
        "app_create",
        "app_update_schema",
        "app_write_html",
        "app_read_data",
        "app_append_row",
        APP_DELETE_TOOL_NAME,
    }


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
        assert appended["row"]["title"] == "A"

        read = json.loads(await read_data.ainvoke({"app_id": app_id, "limit": 10}))
        assert read["total"] == 1
        assert read["rows"] == [{"title": "A", "status": "new", "notes": ""}]
        assert [col["name"] for col in read["columns"]] == ["title", "status", "notes"]

        rejected = json.loads(await append.ainvoke({"app_id": app_id, "row": {"nope": "1"}}))
        assert "error" in rejected

        deleted = json.loads(await delete.ainvoke({"app_id": app_id}))
        assert deleted == {"deleted": app_id}
        assert json.loads(await list_tool.ainvoke({})) == {"apps": []}


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
        assert "owner" in denied["error"]
        assert json.loads(await list_tool.ainvoke({})) == {"apps": []}
        hidden = json.loads(await read_data.ainvoke({"app_id": app_id}))
        assert "owner" in hidden["error"]


async def test_tools_are_scoped_to_the_running_agent(world: World) -> None:
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
    assert "not found" in out["error"]


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
