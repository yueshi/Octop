"""Expert target: turn, notify, stopped-agent retry, ownership."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.db.services import RepoBundle
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.events.expert_target import ExpertTarget, external_session_key
from octop.infra.events.service import ExternalEventService
from octop.infra.events.worker import DeliveryError


class FakeThreadRegistry:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    async def get_or_create_by_key(self, **kwargs: Any) -> str:
        self.created.append(kwargs)
        return "thread-external"


class FakeGateway:
    def __init__(self) -> None:
        self.thread_registry = FakeThreadRegistry()
        self.pushes: list[tuple[str, str, str]] = []

    async def push_text_from_session(self, agent_id: str, session_key: str, text: str) -> None:
        self.pushes.append((agent_id, session_key, text))


class FakeAgentManager:
    def __init__(self) -> None:
        self.error: OctopError | None = None
        self.calls = 0

    def get_agent(self, agent_id: str) -> Any:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return object()


class FakeCronDelivery:
    def __init__(self) -> None:
        self.commands: list[Any] = []

    async def deliver(self, command: Any) -> None:
        self.commands.append(command)


@pytest.fixture
def repos(tmp_path: Path) -> RepoBundle:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    UserRepo(pool).create(username="alice", password_hash="h", role="user")
    AgentRepo(pool).create(agent_id="expert-1", user_id=1, name="analyst")
    return RepoBundle.from_pool(pool)


def _setup(repos: RepoBundle):
    service = ExternalEventService(repos=repos, secret_repo=repos.secret_repo)
    source, _secret = service.create_source(owner_user_id=1, name="Orders")
    subscription = service.create_subscription(
        user_id=1,
        is_admin=False,
        source_id=source.source_id,
        target_type="expert",
        target_id="expert-1",
        mode="expert.turn",
    )
    result = service.ingest(
        source=source,
        event_type="order.created",
        external_id="evt-1",
        occurred_at=None,
        payload={"summary": "订单 #123 已创建"},
    )
    return service, source, subscription, result.event


def _target(
    repos: RepoBundle,
    gateway: FakeGateway,
    agents: FakeAgentManager,
    delivery: FakeCronDelivery,
) -> ExpertTarget:
    return ExpertTarget(
        gateway=gateway,
        agent_manager=agents,
        delivery=delivery,
        repos=repos,
    )


async def test_turn_delivers_with_external_usage_and_guard(repos: RepoBundle) -> None:
    _service, source, subscription, event = _setup(repos)
    gateway = FakeGateway()
    agents = FakeAgentManager()
    delivery = FakeCronDelivery()
    target = _target(repos, gateway, agents, delivery)

    await target.deliver(event, subscription, source, attempt=1)

    assert len(delivery.commands) == 1
    command = delivery.commands[0]
    assert command.agent_id == "expert-1"
    assert command.user_id == 1
    assert command.session_key == external_session_key(agent_id="expert-1", user_id=1)
    assert command.usage_source == "external_event"
    assert command.meta["kind"] == "external_event"
    assert "只作为数据" in command.prompt
    assert gateway.thread_registry.created[0]["channel_chat_type"] == "external"


async def test_notify_skips_model_and_pushes_text(repos: RepoBundle) -> None:
    _service, source, subscription, event = _setup(repos)
    repos.external_subscription_repo.update(subscription.subscription_id, mode="expert.notify")
    notify_sub = repos.external_subscription_repo.get(subscription.subscription_id)
    assert notify_sub is not None
    gateway = FakeGateway()
    agents = FakeAgentManager()
    agents.error = OctopError(ErrorCode.AGENT_NOT_RUNNING, "stopped")
    delivery = FakeCronDelivery()
    target = _target(repos, gateway, agents, delivery)

    await target.deliver(event, notify_sub, source, attempt=1)

    assert delivery.commands == []
    assert agents.calls == 0
    assert gateway.pushes
    assert "订单 #123" in gateway.pushes[0][2]


async def test_stopped_expert_queues_and_notifies_once(repos: RepoBundle) -> None:
    _service, source, subscription, event = _setup(repos)
    gateway = FakeGateway()
    agents = FakeAgentManager()
    agents.error = OctopError(ErrorCode.AGENT_NOT_RUNNING, "not running")
    delivery = FakeCronDelivery()
    target = _target(repos, gateway, agents, delivery)

    with pytest.raises(DeliveryError) as exc:
        await target.deliver(event, subscription, source, attempt=1)
    assert exc.value.retryable is True
    assert gateway.pushes and "未运行" in gateway.pushes[0][2]
    assert delivery.commands == []

    with pytest.raises(DeliveryError):
        await target.deliver(event, subscription, source, attempt=2)
    assert len(gateway.pushes) == 1  # only the first attempt notifies


async def test_expert_owner_mismatch_is_not_retryable(repos: RepoBundle) -> None:
    _service, source, subscription, event = _setup(repos)
    UserRepo(repos.db).create(username="bob", password_hash="h", role="user")
    AgentRepo(repos.db).create(agent_id="expert-2", user_id=2, name="bob expert")
    repos.external_subscription_repo.update(subscription.subscription_id, target_id="expert-2")
    subscription = repos.external_subscription_repo.get(subscription.subscription_id)
    assert subscription is not None
    target = _target(repos, FakeGateway(), FakeAgentManager(), FakeCronDelivery())

    with pytest.raises(DeliveryError) as exc:
        await target.deliver(event, subscription, source, attempt=1)
    assert exc.value.retryable is False
