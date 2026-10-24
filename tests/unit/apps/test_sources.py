"""Unit tests for the gateway-backed external tool runner."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from octop.config import OctopConfig
from octop.infra.apps.sources import GatewayToolRunner
from octop.infra.connectors.service import ConnectorService
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.connectors import ConnectorRepo
from octop.infra.db.repos.secrets import SecretRepo
from octop.infra.db.repos.settings import SettingsRepo
from octop.infra.errors import ErrorCode, OctopError

FAKE_TOOLS: list[dict[str, Any]] = [
    {"name": "map_search", "description": "Search places", "inputSchema": {"type": "object"}},
    {
        "name": "map_route",
        "description": "Plan a route",
        "inputSchema": {},
        "annotations": {"readOnlyHint": False},
    },
]


@pytest.fixture
def runner_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[GatewayToolRunner, ConnectorRepo]:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    with pool.transaction() as conn:
        conn.execute(
            "INSERT INTO users(id,username,password_hash,role,created_at) VALUES (1,'alice','x','user',1)"
        )
    repo = ConnectorRepo(pool)
    repo.create(
        instance_id="conn1",
        user_id=1,
        kind="baidu-map",
        display_name="Map",
        mcp_server_name="baidu_map__conn1",
    )
    repo.create(
        instance_id="conn2",
        user_id=1,
        kind="qcc",
        display_name="QCC",
        mcp_server_name="qcc__conn2",
    )
    service = ConnectorService(
        repo=repo,
        secret_repo=SecretRepo(pool),
        settings_repo=SettingsRepo(pool),
        config=OctopConfig(),
    )
    service.encrypt_and_store(instance_id="conn1", payload={"api_key": "map-key"})

    monkeypatch.setattr("octop.infra.apps.sources.mcp_tools_for_kind", lambda kind: FAKE_TOOLS)
    return GatewayToolRunner(repo=repo, service=service), repo


def _assert_code(exc: pytest.ExceptionInfo[OctopError], code: ErrorCode) -> None:
    assert exc.value.code == code


def test_run_calls_gateway_with_decrypted_creds(
    runner_env: tuple[GatewayToolRunner, ConnectorRepo],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, _repo = runner_env
    seen: dict[str, Any] = {}

    def fake_call(kind: str, creds: dict[str, Any], tool: str, args: dict[str, Any]) -> str:
        seen.update({"kind": kind, "creds": creds, "tool": tool, "args": args})
        return '[{"name": "West Lake"}]'

    monkeypatch.setattr("octop.infra.apps.sources.call_gateway_tool", fake_call)
    output = runner.run(instance_id="conn1", tool="map_search", args={"q": "lake"})
    assert output == '[{"name": "West Lake"}]'
    assert seen["kind"] == "baidu-map"
    assert seen["creds"] == {"api_key": "map-key", "instance_id": "conn1"}
    assert seen["tool"] == "map_search"


def test_run_rejects_unknown_instance_and_non_gateway(
    runner_env: tuple[GatewayToolRunner, ConnectorRepo],
) -> None:
    runner, _repo = runner_env
    with pytest.raises(OctopError) as exc:
        runner.run(instance_id="missing", tool="map_search", args={})
    _assert_code(exc, ErrorCode.APP_SOURCE_INVALID)

    # qcc is an internal-HTTP connector, not callable in-process.
    with pytest.raises(OctopError) as exc:
        runner.run(instance_id="conn2", tool="anything", args={})
    _assert_code(exc, ErrorCode.APP_SOURCE_INVALID)


def test_run_rejects_unread_only_and_unknown_tools(
    runner_env: tuple[GatewayToolRunner, ConnectorRepo],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, _repo = runner_env
    with pytest.raises(OctopError) as exc:
        runner.run(instance_id="conn1", tool="map_route", args={})
    _assert_code(exc, ErrorCode.APP_SOURCE_INVALID)

    with pytest.raises(OctopError) as exc:
        runner.run(instance_id="conn1", tool="no_such_tool", args={})
    _assert_code(exc, ErrorCode.APP_SOURCE_INVALID)


def test_run_rejects_instance_without_creds(
    runner_env: tuple[GatewayToolRunner, ConnectorRepo],
) -> None:
    runner, repo = runner_env
    repo.create(
        instance_id="conn3",
        user_id=1,
        kind="baidu-map",
        display_name="Map 2",
        mcp_server_name="baidu_map__conn3",
    )
    with pytest.raises(OctopError) as exc:
        runner.run(instance_id="conn3", tool="map_search", args={})
    _assert_code(exc, ErrorCode.APP_SOURCE_INVALID)


def test_list_tools_and_normalization(
    runner_env: tuple[GatewayToolRunner, ConnectorRepo],
) -> None:
    runner, _repo = runner_env

    # Non-gateway kinds list nothing (and do not raise) for the binding dialog.
    assert runner.list_tools("conn2") == []
    assert runner.list_tool_meta("conn2") == []
    assert runner.tool_meta("conn2", "anything") is None

    meta = runner.list_tool_meta("conn1")
    assert [item["name"] for item in meta] == ["map_search", "map_route"]
    assert meta[0]["read_only"] is True
    assert meta[0]["input_schema"] == {"type": "object"}
    assert meta[1]["read_only"] is False

    assert runner.tool_meta("conn1", "map_search") == meta[0]
    assert runner.tool_meta("conn1", "nope") is None

    with pytest.raises(OctopError) as exc:
        runner.list_tools("missing")
    _assert_code(exc, ErrorCode.APP_SOURCE_INVALID)
