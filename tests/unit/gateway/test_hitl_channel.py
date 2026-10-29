"""Unit tests for IM channel HITL."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from octop_gateway.models import MessageEvent, MessageEventType, TextContent

from octop.infra.gateway.hitl.coordinator import (
    HitlChannelCoordinator,
    HitlSlashOutcome,
    HitlStreamContext,
)
from octop.infra.gateway.hitl.format import format_hitl_card, parse_action_requests
from octop.infra.gateway.hitl.store import HitlPendingStore
from octop.infra.gateway.process.stream_project import (
    StreamProjectionState,
    project_resume_stream,
    project_stream,
)


def test_parse_action_requests() -> None:
    raw = {
        "action_requests": [
            {"name": "execute", "args": {"command": "ls"}, "description": "run ls"},
        ]
    }
    actions = parse_action_requests(raw)
    assert len(actions) == 1
    assert actions[0]["name"] == "execute"
    assert actions[0]["args"] == {"command": "ls"}


def test_parse_action_requests_unwraps_interrupt_envelope() -> None:
    from octop.infra.gateway.hitl.format import normalize_hitl_request

    raw = {
        "id": "int-1",
        "value": {
            "action_requests": [
                {
                    "name": "ask_user_question",
                    "args": {"questions": [{"question": "Which DB?"}]},
                }
            ],
            "review_configs": [
                {"action_name": "ask_user_question", "allowed_decisions": ["respond"]}
            ],
        },
    }
    assert normalize_hitl_request(raw)["action_requests"][0]["name"] == "ask_user_question"
    actions = parse_action_requests(raw)
    assert actions[0]["name"] == "ask_user_question"
    assert actions[0]["args"]["questions"][0]["question"] == "Which DB?"


def test_hitl_store_register_and_resolve() -> None:
    store = HitlPendingStore()
    record = store.register(
        thread_id="thr1",
        agent_id="agent1",
        user_id=7,
        session_key="sk1",
        channel_type="feishu",
        action_requests=[{"name": "execute", "args": {}}],
        review_configs=None,
    )
    assert record.pending_id
    resolved = store.resolve_for_session("sk1")
    assert resolved is not None
    assert resolved.pending_id == record.pending_id
    store.mark_resolved(record.pending_id, "approved")
    assert store.resolve_for_session("sk1") is None


def test_format_hitl_card_contains_commands() -> None:
    text = format_hitl_card(
        [{"name": "execute", "args": {"command": "echo hi"}}],
        pending_id="ab12",
        locale="en",
    )
    assert "/approve" in text
    assert "/reject" in text
    assert "ab12" in text


@pytest.mark.asyncio
async def test_project_stream_emits_hitl_card() -> None:
    async def _stream(*_args: object, **_kwargs: object):
        yield {
            "type": "hitl_required",
            "request": {
                "action_requests": [{"name": "write_file", "args": {"path": "/tmp/x"}}],
            },
        }

    agent_manager = MagicMock()
    agent_manager.stream = _stream

    coordinator = HitlChannelCoordinator()
    state = StreamProjectionState()
    events: list[MessageEvent] = []
    async for ev in project_stream(
        agent_manager,
        "agent1",
        {"thread_id": "thr1", "messages": []},
        projection_state=state,
        hitl_coordinator=coordinator,
        hitl_ctx=HitlStreamContext(
            thread_id="thr1",
            agent_id="agent1",
            user_id=1,
            session_key="sk1",
            channel_type="feishu",
        ),
    ):
        events.append(ev)

    assert state.hitl_paused is True
    assert state.hitl_pending_id
    assert len(events) == 1
    assert events[0].type == MessageEventType.MESSAGE
    text = events[0].content[0]
    assert isinstance(text, TextContent)
    assert "/approve" in text.text


@pytest.mark.asyncio
async def test_coordinator_slash_approve_resumes() -> None:
    store = HitlPendingStore()
    coordinator = HitlChannelCoordinator(store=store)
    store.register(
        thread_id="thr1",
        agent_id="agent1",
        user_id=1,
        session_key="sk1",
        channel_type="feishu",
        action_requests=[{"name": "execute", "args": {}}],
        review_configs=None,
    )

    async def _resume(*_args: object, **_kwargs: object):
        yield {"type": "token", "content": "done", "node": "agent"}

    agent_manager = MagicMock()
    agent_manager.resume_hitl = _resume

    ctx = MagicMock()
    ctx.session_key = "sk1"
    ctx.agent_id = "agent1"
    ctx.user_id = 1
    ctx.channel_type = "feishu"
    ctx.thread_registry = MagicMock()

    from octop_harness.slash import SlashCommand

    cmd = SlashCommand(name="approve", args="")
    events: list[MessageEvent] = []
    async for ev in coordinator.iter_slash_resolution(
        cmd,
        ctx,
        agent_manager=agent_manager,
        locale="en",
    ):
        events.append(ev)

    assert any(
        isinstance(c, TextContent) and "done" in c.text
        for e in events
        if e.content
        for c in e.content
    )
    assert store.resolve_for_session("sk1") is None


@pytest.mark.asyncio
async def test_coordinator_slash_approve_wakes_team_member_without_host_resume() -> None:
    store = HitlPendingStore()
    coordinator = HitlChannelCoordinator(store=store)
    record = store.register(
        thread_id="thr-room",
        agent_id="host",
        user_id=1,
        session_key="sk1",
        channel_type="feishu",
        action_requests=[{"name": "execute", "args": {}}],
        review_configs=None,
    )
    record.resume_agent_id = "member"
    record.resume_thread_id = "thr-room~member"
    delivered: list[tuple[str, list[dict[str, str]]]] = []

    def deliver(pending_id: str, decisions: list[dict[str, str]]) -> bool:
        delivered.append((pending_id, decisions))
        return True

    agent_manager = MagicMock()
    ctx = MagicMock()
    ctx.session_key = "sk1"
    ctx.agent_id = "host"
    ctx.user_id = 1
    ctx.channel_type = "feishu"
    ctx.thread_registry = MagicMock()

    from octop_harness.slash import SlashCommand

    events: list[MessageEvent] = []
    async for ev in coordinator.iter_slash_resolution(
        SlashCommand(name="approve", args=""),
        ctx,
        agent_manager=agent_manager,
        locale="en",
        member_hitl=deliver,
    ):
        events.append(ev)

    assert delivered == [(record.pending_id, [{"type": "approve"}])]
    assert agent_manager.resume_hitl.call_count == 0
    assert store.get(record.pending_id) is not None
    assert store.get(record.pending_id).status == "approved"  # type: ignore[union-attr]
    assert any(
        isinstance(c, TextContent) and "Approved" in c.text
        for e in events
        if e.content
        for c in e.content
    )


@pytest.mark.asyncio
async def test_coordinator_slash_approve_invalid_pending_id() -> None:
    store = HitlPendingStore()
    coordinator = HitlChannelCoordinator(store=store)
    store.register(
        thread_id="thr1",
        agent_id="agent1",
        user_id=1,
        session_key="sk1",
        channel_type="feishu",
        action_requests=[{"name": "execute", "args": {}}],
        review_configs=None,
    )

    from octop_harness.slash import SlashCommand

    ctx = MagicMock()
    ctx.session_key = "sk1"
    ctx.agent_id = "agent1"
    ctx.user_id = 1

    cmd = SlashCommand(name="approve", args="deadbeef")
    events: list[MessageEvent] = []
    async for ev in coordinator.iter_slash_resolution(
        cmd,
        ctx,
        agent_manager=MagicMock(),
        locale="en",
    ):
        events.append(ev)

    text = "".join(
        c.text for e in events if e.content for c in e.content if isinstance(c, TextContent)
    )
    assert "deadbeef" in text
    assert store.resolve_for_session("sk1") is not None


@pytest.mark.asyncio
async def test_coordinator_resume_failure_keeps_pending() -> None:
    store = HitlPendingStore()
    coordinator = HitlChannelCoordinator(store=store)
    record = store.register(
        thread_id="thr1",
        agent_id="agent1",
        user_id=1,
        session_key="sk1",
        channel_type="feishu",
        action_requests=[{"name": "execute", "args": {}}],
        review_configs=None,
    )

    agent_manager = MagicMock()

    async def _fail(*_args: object, **_kwargs: object):
        raise RuntimeError("boom")
        yield  # pragma: no cover

    agent_manager.resume_hitl = _fail

    ctx = MagicMock()
    ctx.session_key = "sk1"
    ctx.agent_id = "agent1"
    ctx.user_id = 1
    ctx.channel_type = "feishu"
    ctx.thread_registry = MagicMock()

    from octop_harness.slash import SlashCommand

    cmd = SlashCommand(name="approve", args="")
    async for _ in coordinator.iter_slash_resolution(
        cmd,
        ctx,
        agent_manager=agent_manager,
        locale="en",
    ):
        pass

    pending = store.resolve_for_session("sk1")
    assert pending is not None
    assert pending.pending_id == record.pending_id


@pytest.mark.asyncio
async def test_project_resume_stream_nested_hitl() -> None:
    async def _resume(*_args: object, **_kwargs: object):
        yield {
            "type": "hitl_required",
            "request": {
                "action_requests": [{"name": "delete_file", "args": {"path": "/tmp/y"}}],
            },
        }

    agent_manager = MagicMock()
    agent_manager.resume_hitl = _resume

    coordinator = HitlChannelCoordinator()
    state = StreamProjectionState()
    events: list[MessageEvent] = []
    async for ev in project_resume_stream(
        agent_manager,
        "agent1",
        "thr1",
        [{"type": "approve"}],
        projection_state=state,
        hitl_coordinator=coordinator,
        hitl_ctx=HitlStreamContext(
            thread_id="thr1",
            agent_id="agent1",
            user_id=1,
            session_key="sk1",
            channel_type="feishu",
        ),
    ):
        events.append(ev)

    assert state.hitl_paused is True
    assert state.hitl_pending_id
    assert len(events) == 1


def test_hitl_store_gc_removes_stale_resolved() -> None:
    store = HitlPendingStore(ttl_seconds=0.01)
    record = store.register(
        thread_id="thr1",
        agent_id="agent1",
        user_id=1,
        session_key="sk1",
        channel_type="feishu",
        action_requests=[{"name": "execute", "args": {}}],
        review_configs=None,
    )
    store.mark_resolved(record.pending_id, "approved")
    import time

    time.sleep(0.02)
    store._gc()
    assert store.get(record.pending_id) is None


def test_hitl_store_resolve_pending_for_thread() -> None:
    store = HitlPendingStore()
    store.register(
        thread_id="thr-a",
        agent_id="agent1",
        user_id=1,
        session_key="sk1",
        channel_type="dashboard",
        action_requests=[{"name": "execute", "args": {}}],
        review_configs=None,
    )
    store.register(
        thread_id="thr-b",
        agent_id="agent1",
        user_id=1,
        session_key="sk2",
        channel_type="dashboard",
        action_requests=[{"name": "write_file", "args": {"path": "x"}}],
        review_configs=None,
    )
    pending = store.resolve_pending_for_thread("thr-a", agent_id="agent1", user_id=1)
    assert pending is not None
    assert pending.action_requests[0]["name"] == "execute"
    assert store.resolve_pending_for_thread("thr-a", agent_id="agent1", user_id=2) is None
    assert store.pending_thread_ids(agent_id="agent1", user_id=1) == frozenset({"thr-a", "thr-b"})
    assert store.pending_thread_ids(agent_id="agent1", user_id=2) == frozenset()


def test_hitl_store_reuses_equivalent_pending() -> None:
    store = HitlPendingStore()
    actions = [{"name": "execute", "args": {"command": "df"}}]
    first = store.register(
        thread_id="thr-a",
        agent_id="agent1",
        user_id=1,
        session_key="sk",
        channel_type="dashboard",
        action_requests=actions,
        review_configs=None,
    )
    again = store.register(
        thread_id="thr-a",
        agent_id="agent1",
        user_id=1,
        session_key="sk",
        channel_type="dashboard",
        action_requests=[{"args": {"command": "df"}, "name": "execute"}],
        review_configs=None,
    )
    assert again is first
    assert store.get(first.pending_id) is not None
    assert store.get(first.pending_id).status == "pending"  # type: ignore[union-attr]
    replaced = store.register(
        thread_id="thr-a",
        agent_id="agent1",
        user_id=1,
        session_key="sk",
        channel_type="dashboard",
        action_requests=[{"name": "write_file", "args": {}}],
        review_configs=None,
    )
    assert replaced is not first
    assert store.get(first.pending_id).status == "expired"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_session_auto_approve_resumes_without_yielding_the_card() -> None:
    from octop.infra.agents.security.hitl_session import (
        HitlSessionPolicy,
        HitlSessionPolicyStore,
    )
    from octop.infra.gateway.hitl.coordinator import iter_with_session_auto_approve

    policies = HitlSessionPolicyStore()
    policies.set("thr-a", HitlSessionPolicy(mode="allow_all"))
    resumed: list[list[dict[str, object]]] = []

    async def first_turn():
        yield {
            "type": "hitl_required",
            "request": {"action_requests": [{"name": "execute", "args": {}}]},
        }
        yield {"type": "state_update", "data": {}}

    async def resume(decisions: list[dict[str, object]]):
        resumed.append(decisions)
        yield {"type": "token", "content": "done-after-approve"}

    chunks = [
        chunk
        async for chunk in iter_with_session_auto_approve(
            first_turn(),
            thread_id="thr-a",
            policies=policies,
            resume=resume,
        )
    ]
    assert [chunk.get("type") for chunk in chunks] == ["state_update", "token"]
    assert resumed == [[{"type": "approve"}]]


@pytest.mark.asyncio
async def test_session_auto_approve_never_resumes_mandatory_delete() -> None:
    from octop.infra.agents.security.hitl_session import (
        HitlSessionPolicy,
        HitlSessionPolicyStore,
    )
    from octop.infra.gateway.hitl.coordinator import iter_with_session_auto_approve

    policies = HitlSessionPolicyStore()
    policies.set("thr-a", HitlSessionPolicy(mode="allow_all"))
    resumed: list[list[dict[str, object]]] = []

    async def first_turn():
        yield {
            "type": "hitl_required",
            "request": {
                "action_requests": [{"name": "app_delete", "args": {"app_id": "app_1"}}],
                "review_configs": [
                    {
                        "action_name": "app_delete",
                        "allowed_decisions": ["approve", "reject"],
                    },
                ],
            },
        }

    async def resume(decisions: list[dict[str, object]]):
        resumed.append(decisions)
        yield {"type": "token", "content": "should-not-run"}

    chunks = [
        chunk
        async for chunk in iter_with_session_auto_approve(
            first_turn(),
            thread_id="thr-a",
            policies=policies,
            resume=resume,
        )
    ]
    assert [chunk.get("type") for chunk in chunks] == ["hitl_required"]
    assert resumed == []


@pytest.mark.asyncio
async def test_session_auto_approve_yields_card_when_approve_is_forbidden() -> None:
    from octop.infra.agents.security.hitl_session import (
        HitlSessionPolicy,
        HitlSessionPolicyStore,
    )
    from octop.infra.gateway.hitl.coordinator import iter_with_session_auto_approve

    policies = HitlSessionPolicyStore()
    policies.set("thr-a", HitlSessionPolicy(mode="allow_all"))
    resumed: list[list[dict[str, object]]] = []

    async def first_turn():
        yield {
            "type": "hitl_required",
            "request": {
                "action_requests": [{"name": "execute", "args": {}}],
                "review_configs": [
                    {"action_name": "execute", "allowed_decisions": ["reject"]},
                ],
            },
        }

    async def resume(decisions: list[dict[str, object]]):
        resumed.append(decisions)
        yield {"type": "token", "content": "should-not-run"}

    chunks = [
        chunk
        async for chunk in iter_with_session_auto_approve(
            first_turn(),
            thread_id="thr-a",
            policies=policies,
            resume=resume,
        )
    ]
    assert [chunk.get("type") for chunk in chunks] == ["hitl_required"]
    assert resumed == []


@pytest.mark.asyncio
async def test_session_auto_approve_yields_card_when_resume_fails() -> None:
    from octop.infra.agents.security.hitl_session import (
        HitlSessionPolicy,
        HitlSessionPolicyStore,
    )
    from octop.infra.gateway.hitl.coordinator import iter_with_session_auto_approve

    policies = HitlSessionPolicyStore()
    policies.set("thr-a", HitlSessionPolicy(mode="allow_all"))

    async def first_turn():
        yield {
            "type": "hitl_required",
            "request": {"action_requests": [{"name": "execute", "args": {}}]},
        }

    async def resume(_decisions: list[dict[str, object]]):
        raise RuntimeError("graph rejected approve")
        yield {"type": "token", "content": "unused"}  # pragma: no cover

    chunks = [
        chunk
        async for chunk in iter_with_session_auto_approve(
            first_turn(),
            thread_id="thr-a",
            policies=policies,
            resume=resume,
        )
    ]
    assert [chunk.get("type") for chunk in chunks] == ["hitl_required"]


def test_hitl_store_keeps_each_team_member_pause() -> None:
    store = HitlPendingStore()
    first = store.register(
        thread_id="thr-room",
        agent_id="host",
        user_id=1,
        session_key="host:dashboard:1:dm",
        channel_type="dashboard",
        action_requests=[{"name": "execute", "args": {}}],
        review_configs=None,
        resume_agent_id="doctor",
    )
    second = store.register(
        thread_id="thr-room",
        agent_id="host",
        user_id=1,
        session_key="host:dashboard:1:dm",
        channel_type="dashboard",
        action_requests=[{"name": "write_file", "args": {}}],
        review_configs=None,
        resume_agent_id="nurse",
    )
    assert store.get(first.pending_id) is not None
    assert store.get(first.pending_id).status == "pending"  # type: ignore[union-attr]
    assert store.get(second.pending_id) is not None
    assert store.get(second.pending_id).status == "pending"  # type: ignore[union-attr]
    picked = store.resolve_pending_for_thread(
        "thr-room",
        agent_id="host",
        user_id=1,
        pending_id=first.pending_id,
    )
    assert picked is first
    again = store.register(
        thread_id="thr-room",
        agent_id="host",
        user_id=1,
        session_key="host:dashboard:1:dm",
        channel_type="dashboard",
        action_requests=[{"name": "execute", "args": {"command": "df"}}],
        review_configs=None,
        resume_agent_id="doctor",
    )
    assert store.get(first.pending_id).status == "expired"  # type: ignore[union-attr]
    assert store.get(second.pending_id).status == "pending"  # type: ignore[union-attr]
    assert store.get(again.pending_id).status == "pending"  # type: ignore[union-attr]


def test_hitl_store_resolves_team_member_pause_from_either_thread() -> None:
    store = HitlPendingStore()
    record = store.register(
        thread_id="thr-room",
        agent_id="host",
        user_id=1,
        session_key="host:dashboard:1:dm",
        channel_type="dashboard",
        action_requests=[{"name": "execute", "args": {}}],
        review_configs=None,
    )
    record.resume_agent_id = "member"
    record.resume_thread_id = "thr-room~member"
    assert store.resolve_pending_for_thread("thr-room", agent_id="host", user_id=1) is record
    assert (
        store.resolve_pending_for_thread("thr-room~member", agent_id="member", user_id=1) is record
    )
    assert store.pending_thread_ids(agent_id="host", user_id=1) == frozenset({"thr-room"})
    assert store.pending_thread_ids(agent_id="member", user_id=1) == frozenset({"thr-room~member"})


def test_pending_hitl_payload() -> None:
    from octop.infra.gateway.hitl.coordinator import pending_hitl_payload

    store = HitlPendingStore()
    record = store.register(
        thread_id="thr1",
        agent_id="agent1",
        user_id=7,
        session_key="sk1",
        channel_type="dashboard",
        action_requests=[{"name": "execute", "args": {"command": "ls"}}],
        review_configs=None,
    )
    payload = pending_hitl_payload(
        store,
        thread_id="thr1",
        agent_id="agent1",
        user_id=7,
    )
    assert payload is not None
    assert payload["pending_id"] == record.pending_id
    assert payload["action_requests"][0]["name"] == "execute"


def test_hitl_store_get_pending_agent_mismatch() -> None:
    store = HitlPendingStore()
    record = store.register(
        thread_id="thr1",
        agent_id="agent1",
        user_id=1,
        session_key="sk1",
        channel_type="feishu",
        action_requests=[{"name": "execute", "args": {}}],
        review_configs=None,
    )
    assert store.get_pending(record.pending_id, session_key="sk1", agent_id="other") is None


def test_expire_pending_for_thread_drops_history_reinject() -> None:
    store = HitlPendingStore()
    record = store.register(
        thread_id="thr-ask",
        agent_id="agent1",
        user_id=7,
        session_key="sk-ask",
        channel_type="dashboard",
        action_requests=[{"name": "ask_user_question", "args": {"questions": []}}],
        review_configs=None,
    )
    store.expire_pending_for_thread("thr-ask", agent_id="agent1", user_id=7)
    assert store.resolve_pending_for_thread("thr-ask", agent_id="agent1", user_id=7) is None
    expired = store.get(record.pending_id)
    assert expired is not None
    assert expired.status == "expired"


@pytest.mark.asyncio
async def test_slash_outcome_completed_turn() -> None:
    store = HitlPendingStore()
    coordinator = HitlChannelCoordinator(store=store)
    store.register(
        thread_id="thr1",
        agent_id="agent1",
        user_id=1,
        session_key="sk1",
        channel_type="feishu",
        action_requests=[{"name": "execute", "args": {}}],
        review_configs=None,
    )

    async def _resume(*_args: object, **_kwargs: object):
        yield {"type": "token", "content": "ok", "node": "agent"}

    agent_manager = MagicMock()
    agent_manager.resume_hitl = _resume

    ctx = MagicMock()
    ctx.session_key = "sk1"
    ctx.agent_id = "agent1"
    ctx.user_id = 1
    ctx.channel_type = "feishu"
    ctx.thread_registry = MagicMock()

    from octop_harness.slash import SlashCommand

    outcome = HitlSlashOutcome()
    cmd = SlashCommand(name="approve", args="")
    async for _ in coordinator.iter_slash_resolution(
        cmd,
        ctx,
        agent_manager=agent_manager,
        locale="en",
        outcome=outcome,
    ):
        pass
    assert outcome.completed_turn is True


# ---------------------------------------------------------------------------
# Durable hitl_pending mirror (dual-write + boot recovery)
# ---------------------------------------------------------------------------


def _hitl_repo(tmp_path):
    from octop.infra.db.migrate import run_migrations
    from octop.infra.db.pool import SqlitePool
    from octop.infra.db.repos.hitl import HitlPendingRepo

    pool = SqlitePool(tmp_path / "hitl.db")
    run_migrations(pool)
    return HitlPendingRepo(pool)


def _ask_actions(task_id: str = "t1") -> list[dict]:
    return [
        {
            "name": "ask_creator",
            "args": {"questions": [{"question": "A or B?"}], "task_id": task_id},
        }
    ]


def test_store_dual_writes_and_resolves(tmp_path) -> None:
    from types import SimpleNamespace

    repo = _hitl_repo(tmp_path)
    store = HitlPendingStore(repo=repo)
    ctx = SimpleNamespace(
        thread_id="thr-1",
        agent_id="team-01",
        user_id=1,
        session_key="team-01:dashboard:1:dm",
        channel_type="dashboard",
    )
    record = HitlChannelCoordinator(store).register_from_request(
        {"action_requests": _ask_actions("t1")}, ctx=ctx
    )
    assert record.row_pk is not None
    rows = repo.list_pending()
    assert len(rows) == 1
    assert rows[0].task_id == "t1"
    assert rows[0].kind == "ask"
    assert rows[0].payload["agent_id"] == "team-01"
    assert rows[0].payload["pending_id"] == record.pending_id

    store.mark_resolved(record.pending_id, "approved")
    assert repo.list_pending() == []
    row = repo.get(record.row_pk)
    assert row is not None and row.status == "approved"


def test_store_register_dedup_creates_one_row(tmp_path) -> None:
    store = HitlPendingStore(repo=_hitl_repo(tmp_path))
    kwargs = {
        "thread_id": "thr-1",
        "agent_id": "team-01",
        "user_id": 1,
        "session_key": "team-01:dashboard:1:dm",
        "channel_type": "dashboard",
        "action_requests": _ask_actions("t1"),
        "review_configs": None,
    }
    first = store.register(**kwargs)
    again = store.register(**kwargs)
    assert again.pending_id == first.pending_id
    assert len(store.repo.list_pending()) == 1  # type: ignore[union-attr]


def test_store_boot_recovery_rehydrates_pending(tmp_path) -> None:
    repo = _hitl_repo(tmp_path)
    row = repo.create(
        session_key="team-01:dashboard:1:dm",
        thread_id="thr-9",
        kind="ask",
        payload={
            "agent_id": "team-01",
            "user_id": 1,
            "channel_type": "dashboard",
            "action_requests": _ask_actions("t1"),
        },
        created_by_agent="team-01",
        task_id="t1",
    )

    store = HitlPendingStore(repo=repo)
    store.load_persisted()
    record = store.resolve_pending_for_thread("thr-9", agent_id="team-01", user_id=1)
    assert record is not None
    assert record.task_id == "t1"
    assert record.row_pk == row.pk
    assert record.pending_id != row.payload.get("pending_id")  # fresh nonce
    # No duplicate rows were created by the recovery pass.
    assert [r.pk for r in repo.list_pending()] == [row.pk]


def test_store_boot_recovery_keeps_pending_id_answerable(tmp_path) -> None:
    """A recovered approval must stay answerable with the id on its card.

    The card projected into the thread history carries the original
    ``pending_id``; a recovery pass that mints a fresh nonce leaves the card
    permanently unanswerable (the resume route reports ``hitl.expired``).
    """
    repo = _hitl_repo(tmp_path)
    row = repo.create(
        session_key="k",
        thread_id="thr-keep",
        kind="approval",
        payload={
            "pending_id": "orig-1",
            "agent_id": "a1",
            "user_id": 1,
            "action_requests": _ask_actions("t1"),
        },
        created_by_agent="a1",
        task_id="t1",
    )

    store = HitlPendingStore(repo=repo)
    store.load_persisted()

    # The id the card carries must still resolve to a live record.
    record = store.resolve_pending_for_thread(
        "thr-keep", agent_id="a1", user_id=1, pending_id="orig-1"
    )
    assert record is not None
    assert record.pending_id == "orig-1"
    assert record.row_pk == row.pk


def test_store_boot_recovery_expires_stale_rows(tmp_path) -> None:
    repo = _hitl_repo(tmp_path)
    row = repo.create(
        session_key="k",
        thread_id="thr-old",
        kind="approval",
        payload={"agent_id": "a1", "user_id": 1, "action_requests": []},
    )
    with repo._db.connect() as conn:  # noqa: SLF001
        conn.execute(
            "UPDATE hitl_pending SET created_at = ? WHERE id = ?",
            (1, row.pk),
        )

    store = HitlPendingStore(repo=repo, ttl_seconds=30 * 60)
    store.load_persisted()
    assert store.resolve_pending_for_thread("thr-old", agent_id="a1", user_id=1) is None
    assert repo.list_pending() == []
    stale = repo.get(row.pk)
    assert stale is not None and stale.status == "expired"


def test_store_load_persisted_noop_without_repo(tmp_path) -> None:
    store = HitlPendingStore()
    store.load_persisted()  # must not raise
    assert store._records == {}


def test_hitl_store_expire_for_task_only_matching_pauses() -> None:
    store = HitlPendingStore()
    first = store.register(
        thread_id="thr1",
        agent_id="agent1",
        user_id=7,
        session_key="sk1",
        channel_type="dashboard",
        action_requests=[{"name": "ask_creator", "args": {"task_id": "t1"}}],
        review_configs=None,
    )
    second = store.register(
        thread_id="thr2",
        agent_id="agent1",
        user_id=7,
        session_key="sk2",
        channel_type="dashboard",
        action_requests=[{"name": "ask_creator", "args": {"task_id": "t2"}}],
        review_configs=None,
    )

    assert store.expire_for_task("t1") == 1
    assert first.status == "expired"
    assert second.status == "pending"
    assert store.resolve_pending_for_thread("thr2", agent_id="agent1") is not None


def test_hitl_store_attach_task_links_pause() -> None:
    store = HitlPendingStore()
    record = store.register(
        thread_id="thr1",
        agent_id="agent1",
        user_id=7,
        session_key="sk1",
        channel_type="dashboard",
        action_requests=[{"name": "execute", "args": {}}],
        review_configs=None,
    )
    assert record.task_id is None
    store.attach_task(record.pending_id, "t1")
    assert record.task_id == "t1"
    assert store.expire_for_task("t1") == 1


def test_concurrent_creates_bind_their_own_row(tmp_path) -> None:
    """Each inserted pause must own its primary key.

    The old code read ``SELECT MAX(id)`` *after* the insert, outside its
    transaction, so a concurrent register could bind this record to another
    row's pk: resolving one pause resolved the other and the loser stayed
    pending forever.

    Caveat: SQLite's ``BEGIN IMMEDIATE`` serialises the write transactions,
    so this test cannot reliably interleave two ``create`` calls and does not
    by itself prove the fix — the race is real under PostgreSQL with a
    connection pool, where the post-insert SELECT is not ordered against the
    other connection's INSERT. ``RETURNING id`` removes the window entirely;
    that is the actual guarantee.
    """
    from concurrent.futures import ThreadPoolExecutor

    from octop.infra.db.migrate import run_migrations
    from octop.infra.db.pool import SqlitePool
    from octop.infra.db.repos.hitl import HitlPendingRepo

    path = tmp_path / "hitl.db"
    run_migrations(SqlitePool(path))
    # One connection per writer: a shared pool would serialise them and hide
    # the interleaving this test exists to catch.
    repos = [HitlPendingRepo(SqlitePool(path)) for _ in range(4)]

    def _create(i: int) -> tuple[int, object]:
        return i, repos[i].create(
            session_key=f"s{i}",
            thread_id=f"t{i}",
            kind="approval",
            payload={"n": i},
        )

    for round_no in range(8):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(_create, range(4)))
        for i, row in results:
            assert row.session_key == f"s{i}", f"round {round_no}: row {i} bound a foreign pk"
            assert row.thread_id == f"t{i}", f"round {round_no}: row {i} bound a foreign pk"
