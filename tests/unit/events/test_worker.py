"""Outbox worker: app/team targets, retry/backoff, lease recovery."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.db.services import RepoBundle
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.events.service import ExternalEventService
from octop.infra.events.worker import ExternalEventWorker


@pytest.fixture
def repos(tmp_path: Path) -> RepoBundle:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    UserRepo(pool).create(username="alice", password_hash="h", role="user")
    AgentRepo(pool).create(agent_id="team-1", user_id=1, name="crew", kind="team")
    AgentRepo(pool).create(agent_id="expert-1", user_id=1, name="analyst")
    return RepoBundle.from_pool(pool)


@pytest.fixture
def service(repos: RepoBundle) -> ExternalEventService:
    return ExternalEventService(repos=repos, secret_repo=repos.secret_repo)


def _source(service: ExternalEventService):
    row, _secret = service.create_source(owner_user_id=1, name="Orders")
    return row


def _subscription(
    repos: RepoBundle,
    *,
    source_id: str,
    target_type: str,
    target_id: str,
    mode: str,
    mapping: dict[str, Any] | None = None,
    coalesce_seconds: int = 0,
):
    return repos.external_subscription_repo.create(
        source_id=source_id,
        target_type=target_type,
        target_id=target_id,
        mode=mode,
        created_by=1,
        mapping_json=json.dumps(mapping or {}),
        coalesce_seconds=coalesce_seconds,
    )


def _ingest(service: ExternalEventService, source, payload: dict[str, Any]):
    return service.ingest(
        source=source,
        event_type="order.created",
        external_id="evt-1",
        occurred_at=None,
        payload=payload,
    )


class FakeAppService:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.row = SimpleNamespace(app_id="app-1", owner_user_id=1)
        self.calls: list[tuple[Any, ...]] = []
        self.fail_first = fail_first

    def get(self, app_id: str) -> Any:
        return self.row

    def permission_for(self, row: Any, user_id: int, *, is_admin: bool) -> str:
        return "edit_structure"

    async def import_rows(
        self,
        row: Any,
        records: list[dict[str, str]],
        *,
        table: str = "main",
        mode: str = "append",
        mapping: Any = None,
    ) -> Any:
        self.calls.append(("import", records, table, mode, mapping))
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError("transient upstream")
        return SimpleNamespace(imported=len(records), skipped=0, errors=[])

    async def run_source(self, row: Any, source_id: str) -> Any:
        self.calls.append(("refresh", source_id))
        return SimpleNamespace(imported=1, skipped=0, errors=[])


class FakeCoordinator:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.autopilot = True

    def is_autopilot(self, team_id: str) -> bool:
        return self.autopilot

    def record(
        self,
        team_id: str,
        *,
        type: str,
        dedupe_key: str,
        actor: str = "",
        payload: dict[str, Any] | None = None,
        route: bool = True,
    ) -> str:
        self.records.append(
            {
                "team_id": team_id,
                "type": type,
                "dedupe_key": dedupe_key,
                "actor": actor,
                "payload": payload or {},
            }
        )
        return "event-1"


def _worker(
    repos: RepoBundle,
    *,
    app_service: Any | None = None,
    coordinator: Any | None = None,
    resolve_team_target: Any | None = None,
    push_user: Any | None = None,
    expert_delivery: Any | None = None,
) -> ExternalEventWorker:
    return ExternalEventWorker(
        repos=repos,
        app_service=app_service,
        coordinator=coordinator,
        resolve_team_target=resolve_team_target,
        push_user=push_user,
        expert_delivery=expert_delivery,
        interval_seconds=60,
    )


async def test_app_import_delivery(repos: RepoBundle, service: ExternalEventService) -> None:
    source = _source(service)
    _subscription(
        repos,
        source_id=source.source_id,
        target_type="app",
        target_id="app-1",
        mode="app.import",
        mapping={"table": "main", "columns": {"name": "title"}},
    )
    _ingest(service, source, {"summary": "two rows", "records": [{"name": "A"}]})
    app = FakeAppService()
    worker = _worker(repos, app_service=app)

    assert await worker.tick() == 1
    await worker.drain()

    assert app.calls == [("import", [{"name": "A"}], "main", "replace", {"name": "title"})]
    delivery = repos.external_delivery_repo.list_for_owner(1)[0]
    assert delivery.status == "delivered"
    assert delivery.attempt == 1


async def test_app_refresh_uses_mapping_source(
    repos: RepoBundle, service: ExternalEventService
) -> None:
    source = _source(service)
    _subscription(
        repos,
        source_id=source.source_id,
        target_type="app",
        target_id="app-1",
        mode="app.refresh",
        mapping={"source_id": "app-src-1"},
    )
    _ingest(service, source, {"summary": "refresh"})
    app = FakeAppService()
    worker = _worker(repos, app_service=app)

    await worker.tick()
    await worker.drain()
    assert app.calls == [("refresh", "app-src-1")]


async def test_expert_turn_delivery(repos: RepoBundle, service: ExternalEventService) -> None:
    source = _source(service)
    _subscription(
        repos,
        source_id=source.source_id,
        target_type="expert",
        target_id="expert-1",
        mode="expert.turn",
    )
    _ingest(service, source, {"summary": "订单 #9"})
    calls: list[tuple[str, str, int]] = []

    async def expert(event: Any, subscription: Any, source: Any, attempt: int) -> None:
        calls.append((event.summary, subscription.target_id, attempt))

    worker = _worker(repos, expert_delivery=expert)
    await worker.tick()
    await worker.drain()

    assert calls == [("订单 #9", "expert-1", 1)]
    delivery = repos.external_delivery_repo.list_for_owner(1)[0]
    assert delivery.status == "delivered"


async def test_expert_turn_coalesces_events(
    repos: RepoBundle, service: ExternalEventService
) -> None:
    source = _source(service)
    _subscription(
        repos,
        source_id=source.source_id,
        target_type="expert",
        target_id="expert-1",
        mode="expert.turn",
        coalesce_seconds=1,
    )
    for external_id, summary in (("e1", "订单 #1"), ("e2", "订单 #2")):
        service.ingest(
            source=source,
            event_type="order.created",
            external_id=external_id,
            occurred_at=None,
            payload={"summary": summary},
        )
    calls: list[str] = []

    async def expert(event: Any, subscription: Any, source: Any, attempt: int) -> None:
        calls.append(event.summary)

    worker = _worker(repos, expert_delivery=expert)
    await worker.tick()
    await worker.drain()

    assert len(calls) == 1
    assert "订单 #1" in calls[0] and "订单 #2" in calls[0]
    deliveries = repos.external_delivery_repo.list_for_owner(1)
    assert {item.status for item in deliveries} == {"delivered"}


async def test_team_judge_records_external_event(
    repos: RepoBundle, service: ExternalEventService
) -> None:
    source = _source(service)
    _subscription(
        repos,
        source_id=source.source_id,
        target_type="team",
        target_id="team-1",
        mode="team.judge",
    )
    event = _ingest(service, source, {"summary": "订单 #123 已创建"})
    coordinator = FakeCoordinator()
    worker = _worker(repos, coordinator=coordinator)

    await worker.tick()
    await worker.drain()

    assert len(coordinator.records) == 1
    record = coordinator.records[0]
    assert record["team_id"] == "team-1"
    assert record["type"] == "external.event"
    assert record["dedupe_key"] == f"external:{event.event.event_id}"
    assert record["payload"]["summary"] == "订单 #123 已创建"
    assert repos.external_delivery_repo.list_for_owner(1)[0].status == "delivered"


async def test_team_judge_rejects_manual_team(
    repos: RepoBundle, service: ExternalEventService
) -> None:
    source = _source(service)
    _subscription(
        repos,
        source_id=source.source_id,
        target_type="team",
        target_id="team-1",
        mode="team.judge",
    )
    _ingest(service, source, {"summary": "ignored"})
    coordinator = FakeCoordinator()
    coordinator.autopilot = False
    worker = _worker(repos, coordinator=coordinator)

    await worker.tick()
    await worker.drain()

    delivery = repos.external_delivery_repo.list_for_owner(1)[0]
    assert delivery.status == "dead"
    assert "autopilot" in delivery.last_error
    assert coordinator.records == []


async def test_team_notify_pushes_dashboard_frame(
    repos: RepoBundle, service: ExternalEventService
) -> None:
    source = _source(service)
    _subscription(
        repos,
        source_id=source.source_id,
        target_type="team",
        target_id="team-1",
        mode="team.notify",
    )
    _ingest(service, source, {"summary": "订单 #9"})
    pushes: list[tuple[int, dict[str, Any]]] = []

    async def resolve(team_id: str) -> Any:
        return SimpleNamespace(user_id=1, thread_id="thread-1")

    async def push(user_id: int, frame: dict[str, Any]) -> None:
        pushes.append((user_id, frame))

    worker = _worker(repos, resolve_team_target=resolve, push_user=push)
    await worker.tick()
    await worker.drain()

    assert pushes and pushes[0][0] == 1
    assert pushes[0][1]["type"] == "dashboard_push"
    assert pushes[0][1]["thread_id"] == "thread-1"
    assert "订单 #9" in pushes[0][1]["text"]


async def test_retryable_failure_requeues_then_succeeds(
    repos: RepoBundle, service: ExternalEventService
) -> None:
    source = _source(service)
    _subscription(
        repos,
        source_id=source.source_id,
        target_type="app",
        target_id="app-1",
        mode="app.import",
    )
    _ingest(service, source, {"summary": "retry", "records": [{"name": "A"}]})
    app = FakeAppService(fail_first=True)
    worker = _worker(repos, app_service=app)

    await worker.tick()
    await worker.drain()
    delivery = repos.external_delivery_repo.list_for_owner(1)[0]
    assert delivery.status == "failed"
    assert delivery.attempt == 1
    assert delivery.next_retry_at is not None

    assert await worker.tick(now=delivery.next_retry_at) == 1
    await worker.drain()
    delivery = repos.external_delivery_repo.get(delivery.delivery_id)
    assert delivery is not None
    assert delivery.status == "delivered"
    assert delivery.attempt == 2


async def test_disabled_subscription_dead_letters(
    repos: RepoBundle, service: ExternalEventService
) -> None:
    source = _source(service)
    sub = _subscription(
        repos,
        source_id=source.source_id,
        target_type="app",
        target_id="app-1",
        mode="app.import",
    )
    _ingest(service, source, {"summary": "x", "records": [{"name": "A"}]})
    repos.external_subscription_repo.update(sub.subscription_id, enabled=False)
    app = FakeAppService()
    worker = _worker(repos, app_service=app)
    await worker.tick()
    await worker.drain()
    delivery = repos.external_delivery_repo.list_for_owner(1)[0]
    assert delivery.status == "dead"
    assert "disabled" in delivery.last_error
    assert app.calls == []


async def test_app_import_rejects_empty_records(
    repos: RepoBundle, service: ExternalEventService
) -> None:
    source = _source(service)
    _subscription(
        repos,
        source_id=source.source_id,
        target_type="app",
        target_id="app-1",
        mode="app.import",
    )
    _ingest(service, source, {"summary": "empty", "records": []})
    app = FakeAppService()
    worker = _worker(repos, app_service=app)
    await worker.tick()
    await worker.drain()
    delivery = repos.external_delivery_repo.list_for_owner(1)[0]
    assert delivery.status == "dead"
    assert "records" in delivery.last_error
    assert app.calls == []


async def test_non_retryable_failure_dead_letters(
    repos: RepoBundle, service: ExternalEventService
) -> None:
    source = _source(service)
    _subscription(
        repos,
        source_id=source.source_id,
        target_type="app",
        target_id="app-1",
        mode="app.import",
    )
    _ingest(service, source, {"summary": "bad payload"})

    class BadApp(FakeAppService):
        async def import_rows(self, *args: Any, **kwargs: Any) -> Any:
            raise OctopError(ErrorCode.APP_ROW_INVALID, "bad rows")

    worker = _worker(repos, app_service=BadApp())
    await worker.tick()
    await worker.drain()
    delivery = repos.external_delivery_repo.list_for_owner(1)[0]
    assert delivery.status == "dead"
    assert delivery.attempt == 1
    assert delivery.next_retry_at is None


async def test_expired_lease_is_requeued(repos: RepoBundle, service: ExternalEventService) -> None:
    source = _source(service)
    _subscription(
        repos,
        source_id=source.source_id,
        target_type="app",
        target_id="app-1",
        mode="app.import",
    )
    _ingest(service, source, {"summary": "lease", "records": [{"name": "A"}]})
    delivery = repos.external_delivery_repo.list_for_owner(1)[0]
    assert repos.external_delivery_repo.claim(delivery.delivery_id, now=100, lease_seconds=10)
    assert repos.external_delivery_repo.requeue_expired(111) == 1
    row = repos.external_delivery_repo.get(delivery.delivery_id)
    assert row is not None
    assert row.status == "failed"
    assert row.next_retry_at == 111
