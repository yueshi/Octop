"""Connector polling: config, cursor, dedupe, backoff, retention."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.connectors import ConnectorRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.db.services import RepoBundle
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.events.connector_poll import parse_poll_config, poll_source
from octop.infra.events.service import ExternalEventService
from octop.infra.events.worker import ExternalEventWorker

CONFIG: dict[str, Any] = {
    "instance_id": "conn-1",
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
RESULT = {
    "data": {
        "items": [
            {"id": "1", "title": "A", "updated_at": 100},
            {"id": "2", "title": "B", "updated_at": 200},
        ]
    }
}


class FakeRunner:
    def __init__(self, *, result: Any = None, error: Exception | None = None) -> None:
        self.result = RESULT if result is None else result
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def run(self, *, instance_id: str, tool: str, args: dict[str, Any]) -> str:
        self.calls.append({"instance_id": instance_id, "tool": tool, "args": args})
        if self.error is not None:
            raise self.error
        return json.dumps(self.result)

    def list_tool_meta(self, instance_id: str) -> list[dict[str, Any]]:
        return [
            {
                "name": "list_orders",
                "description": "",
                "read_only": True,
                "input_schema": {},
            }
        ]

    def tool_meta(self, instance_id: str, tool: str) -> dict[str, Any] | None:
        return self.list_tool_meta(instance_id)[0] if tool == "list_orders" else None


@pytest.fixture
def repos(tmp_path: Path) -> RepoBundle:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    UserRepo(pool).create(username="alice", password_hash="h", role="user")
    ConnectorRepo(pool).create(
        instance_id="conn-1",
        user_id=1,
        kind="feishu",
        display_name="Connector",
        mcp_server_name="feishu",
    )
    return RepoBundle.from_pool(pool)


def _service(repos: RepoBundle, runner: FakeRunner) -> ExternalEventService:
    return ExternalEventService(
        repos=repos,
        secret_repo=repos.secret_repo,
        connector_runner=runner,
    )


def _source(repos: RepoBundle, service: ExternalEventService):
    row, _secret = service.create_source(
        owner_user_id=1,
        name="Orders poll",
        kind="connector_poll",
        config=CONFIG,
    )
    return row


def _worker(repos: RepoBundle, service: ExternalEventService, runner: FakeRunner):
    return ExternalEventWorker(
        repos=repos,
        app_service=None,
        event_service=service,
        connector_runner=runner,
        interval_seconds=60,
    )


def test_parse_poll_config_validates_interval() -> None:
    assert parse_poll_config(CONFIG).interval_seconds == 60
    with pytest.raises(OctopError) as exc:
        parse_poll_config({**CONFIG, "interval_seconds": 1})
    assert exc.value.code is ErrorCode.EXTERNAL_SOURCE_INVALID


def test_create_poll_source_requires_read_only_tool(repos: RepoBundle) -> None:
    class WriteRunner(FakeRunner):
        def tool_meta(self, instance_id: str, tool: str) -> dict[str, Any] | None:
            return {"name": tool, "read_only": False, "input_schema": {}}

    service = _service(repos, WriteRunner())
    with pytest.raises(OctopError) as exc:
        service.create_source(
            owner_user_id=1,
            name="bad poll",
            kind="connector_poll",
            config=CONFIG,
        )
    assert exc.value.code is ErrorCode.EXTERNAL_SOURCE_INVALID


def test_poll_source_advances_cursor_and_dedupes(repos: RepoBundle) -> None:
    runner = FakeRunner()
    service = _service(repos, runner)
    source = _source(repos, service)

    first = poll_source(service=service, source=source, runner=runner, now=1000)
    assert first.items == 2
    assert first.events == 2
    assert first.cursor == 200
    assert "since" not in runner.calls[0]["args"]

    repos.external_source_repo.update(
        source.source_id, cursor_json=json.dumps({"cursor": first.cursor})
    )
    source = repos.external_source_repo.get(source.source_id)
    assert source is not None
    second = poll_source(service=service, source=source, runner=runner, now=1060)
    assert second.events == 0
    assert runner.calls[1]["args"]["since"] == 200
    assert second.cursor == 200


async def test_worker_poll_success_records_state(
    repos: RepoBundle,
) -> None:
    runner = FakeRunner()
    service = _service(repos, runner)
    source = _source(repos, service)
    repos.external_source_repo.update(source.source_id, next_run_at=0)
    worker = _worker(repos, service, runner)

    assert await worker.poll_tick(now=1000) == 1
    await worker.drain()

    updated = repos.external_source_repo.get(source.source_id)
    assert updated is not None
    assert updated.fail_count == 0
    assert updated.last_error == ""
    assert updated.last_poll_at is not None
    assert updated.next_run_at == 1060
    assert json.loads(updated.cursor_json)["cursor"] == 200


async def test_worker_poll_failure_backs_off(repos: RepoBundle) -> None:
    runner = FakeRunner(error=RuntimeError("upstream down"))
    service = _service(repos, runner)
    source = _source(repos, service)
    repos.external_source_repo.update(source.source_id, next_run_at=0)
    worker = _worker(repos, service, runner)

    await worker.poll_tick(now=1000)
    await worker.drain()

    updated = repos.external_source_repo.get(source.source_id)
    assert updated is not None
    assert updated.fail_count == 1
    assert "upstream down" in updated.last_error
    assert updated.next_run_at is not None and updated.next_run_at > 1000


def test_cleanup_removes_expired_events(repos: RepoBundle) -> None:
    service = _service(repos, FakeRunner())
    source = _source(repos, service)
    result = service.ingest(
        source=source,
        event_type="order.updated",
        external_id="old-1",
        occurred_at=None,
        payload={"summary": "old"},
    )
    with repos.db.connect() as conn:
        conn.execute(
            "UPDATE external_events SET received_at = 100 WHERE event_id = ?",
            (result.event.event_id,),
        )
    worker = ExternalEventWorker(repos=repos, app_service=None)
    assert worker.cleanup(now=100 + 30 * 24 * 3600 + 1) == 1
    assert repos.external_event_repo.get(result.event.event_id) is None
