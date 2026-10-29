"""External event service: sources, subscriptions, filtering, dedupe."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.team_runtime import TeamRuntimeRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.db.services import RepoBundle
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.events.security import sign
from octop.infra.events.service import ExternalEventService, matches_filter


@pytest.fixture
def repos(tmp_path: Path) -> RepoBundle:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    UserRepo(pool).create(username="alice", password_hash="h", role="user")
    return RepoBundle.from_pool(pool)


@pytest.fixture
def service(repos: RepoBundle) -> ExternalEventService:
    return ExternalEventService(repos=repos, secret_repo=repos.secret_repo)


def _source(service: ExternalEventService):
    row, _secret = service.create_source(owner_user_id=1, name="Orders")
    return row


def test_source_secret_and_webhook_verification(service: ExternalEventService) -> None:
    row, secret = service.create_source(owner_user_id=1, name="Orders")
    assert row.source_id
    assert secret
    assert row.secret_blob

    body = b'{"summary":"hello"}'
    timestamp = str(int(time.time()))
    source = service.verify_webhook(
        source_id=row.source_id,
        timestamp=timestamp,
        signature=sign(secret, timestamp, body),
        body=body,
    )
    assert source.source_id == row.source_id

    with pytest.raises(OctopError) as exc:
        service.verify_webhook(
            source_id=row.source_id,
            timestamp=timestamp,
            signature="bad",
            body=body,
        )
    assert exc.value.code is ErrorCode.EXTERNAL_SIGNATURE_INVALID

    stale = str(int(time.time()) - 301)
    with pytest.raises(OctopError) as exc:
        service.verify_webhook(
            source_id=row.source_id,
            timestamp=stale,
            signature=sign(secret, stale, body),
            body=body,
        )
    assert exc.value.code is ErrorCode.EXTERNAL_TIMESTAMP_INVALID


def test_ingest_matches_filters_and_dedupes(
    repos: RepoBundle, service: ExternalEventService
) -> None:
    source = _source(service)
    repos.external_subscription_repo.create(
        source_id=source.source_id,
        target_type="team",
        target_id="team-1",
        mode="team.judge",
        created_by=1,
        filter_json=json.dumps({"event_type": "order.created", "equals": {"region": "east"}}),
    )
    repos.external_subscription_repo.create(
        source_id=source.source_id,
        target_type="team",
        target_id="team-2",
        mode="team.judge",
        created_by=1,
        filter_json=json.dumps({"event_type": "order.refunded"}),
    )

    first = service.ingest(
        source=source,
        event_type="order.created",
        external_id="evt-1",
        occurred_at=None,
        payload={"summary": "订单 #1", "region": "east"},
    )
    assert not first.duplicate
    assert first.delivery_count == 1
    assert first.event.summary == "订单 #1"
    assert first.event.status == "dispatched"
    deliveries = repos.external_delivery_repo.list_by_event(first.event.event_id)
    assert len(deliveries) == 1
    assert deliveries[0].target_id == "team-1"

    again = service.ingest(
        source=source,
        event_type="order.created",
        external_id="evt-1",
        occurred_at=None,
        payload={"summary": "duplicate"},
    )
    assert again.duplicate
    assert again.event.event_id == first.event.event_id
    assert again.delivery_count == 0

    listed = service.list_events(user_id=1, is_admin=False, source_id=source.source_id)
    assert [row.event_id for row in listed] == [first.event.event_id]


def test_ingest_without_subscribers(repos: RepoBundle, service: ExternalEventService) -> None:
    source = _source(service)
    result = service.ingest(
        source=source,
        event_type="order.created",
        external_id="evt-2",
        occurred_at=None,
        payload={"summary": "no one listening"},
    )
    assert result.event.status == "no_subscribers"


def test_filter_rules() -> None:
    class Row:
        filter_json = json.dumps(
            {
                "event_type": "order.created",
                "equals": {"status": "paid"},
                "contains": {"note": "vip"},
            }
        )

    row = Row()
    assert matches_filter(row, "order.created", {"status": "paid", "note": "a vip order"})  # type: ignore[arg-type]
    assert not matches_filter(row, "order.refunded", {"status": "paid", "note": "vip"})  # type: ignore[arg-type]
    assert not matches_filter(row, "order.created", {"status": "new", "note": "vip"})  # type: ignore[arg-type]


def test_subscription_target_validation(repos: RepoBundle, service: ExternalEventService) -> None:
    source = _source(service)
    with pytest.raises(OctopError) as exc:
        service.create_subscription(
            user_id=1,
            is_admin=False,
            source_id=source.source_id,
            target_type="expert",
            target_id="missing-expert",
            mode="expert.turn",
        )
    assert exc.value.code is ErrorCode.EXTERNAL_TARGET_INVALID

    AgentRepo(repos.db).create(agent_id="expert-1", user_id=1, name="analyst")
    expert_sub = service.create_subscription(
        user_id=1,
        is_admin=False,
        source_id=source.source_id,
        target_type="expert",
        target_id="expert-1",
        mode="expert.turn",
    )
    assert expert_sub.mode == "expert.turn"

    AgentRepo(repos.db).create(agent_id="team-1", user_id=1, name="crew", kind="team")
    with pytest.raises(OctopError) as exc:
        service.create_subscription(
            user_id=1,
            is_admin=False,
            source_id=source.source_id,
            target_type="team",
            target_id="team-1",
            mode="team.judge",
        )
    assert exc.value.code is ErrorCode.EXTERNAL_SUBSCRIPTION_INVALID
    assert "autopilot" in exc.value.message

    runtime = TeamRuntimeRepo(repos.db).ensure("team-1")
    repos.team_runtime_repo.update(
        "team-1", expected_version=runtime.version, fields={"mode": "autopilot"}
    )
    sub = service.create_subscription(
        user_id=1,
        is_admin=False,
        source_id=source.source_id,
        target_type="team",
        target_id="team-1",
        mode="team.judge",
    )
    assert sub.mode == "team.judge"


def test_source_ownership(repos: RepoBundle, service: ExternalEventService) -> None:
    source = _source(service)
    UserRepo(repos.db).create(username="bob", password_hash="h", role="user")
    with pytest.raises(OctopError) as exc:
        service.get_source(user_id=2, is_admin=False, source_id=source.source_id)
    assert exc.value.code is ErrorCode.EXTERNAL_SOURCE_NOT_FOUND
    assert service.get_source(user_id=2, is_admin=True, source_id=source.source_id)
