"""Web → IM bridge (design B) — session resolution and thread lifecycle frames."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from octop_gateway.models import (
    ChannelSubject,
    ImageContent,
    InboundMessage,
    MessageEvent,
    MessageEventType,
    TextContent,
)

from octop.api.routers.chat.models import ChatTurnBody
from octop.api.routers.chat.turn import (
    PreparedDashboardTurn,
    build_im_bridge_inbound,
    resolve_im_bridge_session,
)
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.gateway.gateway import Gateway
from octop.infra.gateway.process.processor import (
    GlobalProcessor,
    _ChannelOutputTracker,
)
from octop.infra.gateway.ws import WebSocketHub


def _thread(
    *,
    agent_id: str = "a1",
    user_id: int = 7,
    channel_type: str = "feishu",
    session_key: str = "a1:feishu:ou_x:dm",
) -> SimpleNamespace:
    return SimpleNamespace(
        agent_id=agent_id,
        user_id=user_id,
        channel_type=channel_type,
        session_key=session_key,
        title="Feishu chat",
        last_active=1,
    )


def _session(
    *,
    session_key: str = "a1:feishu:ou_x:dm",
    channel_id: str = "ch-feishu",
    channel_type: str = "feishu",
    chat_type: str = "dm",
    user_id: int = 7,
) -> SimpleNamespace:
    subject = ChannelSubject(subject_id="ou_x", chat_type=chat_type)
    return SimpleNamespace(
        session_key=session_key,
        channel_id=channel_id,
        channel_type=channel_type,
        chat_type=chat_type,
        user_id=user_id,
        channel_subject_id="ou_x",
        channel_chat_type=chat_type,
        channel_metadata={
            "channel_type": channel_type,
            "user_id": user_id,
            "chat_id": "oc_1",
            "to_handle": "ou_x",
            "chat_type": chat_type,
        },
        to_channel_subject=lambda: subject,
    )


def _server(
    *,
    row: SimpleNamespace | None,
    sessions: list[SimpleNamespace],
    connected: bool = True,
) -> SimpleNamespace:
    registry = SimpleNamespace(
        get_thread=lambda _tid: row,
        im_sessions_for_thread=lambda _tid: sessions,
    )
    manager = SimpleNamespace(
        get_channel=lambda _cid: object() if connected else None,
    )
    gateway = SimpleNamespace(thread_registry=registry, channel_manager=manager)
    return SimpleNamespace(app_runtime=SimpleNamespace(gateway=gateway))


def test_resolve_im_bridge_session_prefers_thread_session() -> None:
    primary = _session(session_key="a1:feishu:ou_x:dm")
    other = _session(session_key="a1:feishu:ou_y:dm", channel_id="ch-other")
    row = _thread()
    server = _server(row=row, sessions=[other, primary])

    resolved = resolve_im_bridge_session(server, agent_id="a1", user_id=7, thread_id="thr_1")

    assert resolved is primary


def test_resolve_im_bridge_session_rejects_virtual_thread() -> None:
    server = _server(
        row=_thread(channel_type="dashboard", session_key="a1:dashboard:7:dm"),
        sessions=[],
    )
    with pytest.raises(OctopError) as err:
        resolve_im_bridge_session(server, agent_id="a1", user_id=7, thread_id="thr_1")
    assert err.value.code == ErrorCode.CHAT_IM_UNAVAILABLE


def test_resolve_im_bridge_session_rejects_group_session() -> None:
    server = _server(
        row=_thread(session_key="a1:feishu:oc_group:group"),
        sessions=[_session(session_key="a1:feishu:oc_group:group", chat_type="group")],
    )
    with pytest.raises(OctopError) as err:
        resolve_im_bridge_session(server, agent_id="a1", user_id=7, thread_id="thr_1")
    assert err.value.code == ErrorCode.CHAT_IM_UNAVAILABLE


def test_resolve_im_bridge_session_rejects_disconnected_channel() -> None:
    server = _server(row=_thread(), sessions=[_session()], connected=False)
    with pytest.raises(OctopError) as err:
        resolve_im_bridge_session(server, agent_id="a1", user_id=7, thread_id="thr_1")
    assert err.value.code == ErrorCode.CHAT_IM_UNAVAILABLE


def test_resolve_im_bridge_session_rejects_foreign_thread() -> None:
    server = _server(row=_thread(user_id=99), sessions=[_session()])
    with pytest.raises(OctopError) as err:
        resolve_im_bridge_session(server, agent_id="a1", user_id=7, thread_id="thr_1")
    assert err.value.code == ErrorCode.AGENT_NOT_FOUND


def test_build_im_bridge_inbound_marks_web_origin() -> None:
    turn = ChatTurnBody(
        messages=[{"role": "user", "content": "check the dashboard"}],
        thread_id="thr_1",
        reasoning_mode="enabled",
    )
    prepared = PreparedDashboardTurn(
        thread_id="thr_1",
        session_key="a1:dashboard:7:dm",
        mcp_servers=[],
        skills=None,
        model_ref="provider/model",
        inbound_content=[TextContent(text="check the dashboard")],
        composer_context=None,
        inbound_attachments=[],
    )
    session = _session()

    inbound = build_im_bridge_inbound(
        agent_id="a1",
        user_id=7,
        prepared=prepared,
        turn=turn,
        session=session,
        ws_connection_id="conn-1",
    )

    assert inbound.channel_id == "ch-feishu"
    assert inbound.channel_type == "feishu"
    assert inbound.tenant_id == "a1"
    assert inbound.channel_session_id == "a1:feishu:ou_x:dm"
    assert inbound.channel_subject is not None
    assert inbound.channel_subject.subject_id == "ou_x"
    assert inbound.metadata["origin"] == "web"
    assert inbound.metadata["chat_id"] == "oc_1"
    assert inbound.metadata["to_handle"] == "ou_x"
    assert inbound.metadata["thread_id"] == "thr_1"
    assert inbound.metadata["session_key"] == session.session_key
    assert inbound.metadata["ws_connection_id"] == "conn-1"
    assert inbound.metadata["model"] == "provider/model"


def test_ws_payload_reply_via_defaults_to_web_and_parses_im() -> None:
    assert ChatTurnBody.from_ws_payload({"text": "hi"}).reply_via == "web"
    assert ChatTurnBody.from_ws_payload({"text": "hi", "reply_via": "im"}).reply_via == "im"
    assert ChatTurnBody.from_ws_payload({"text": "hi", "reply_via": "bogus"}).reply_via == "web"


@pytest.mark.asyncio
async def test_publish_thread_updated_pushes_turn_frames_and_unread() -> None:
    hub = WebSocketHub()
    chat_frames: list[dict[str, Any]] = []
    user_frames: list[dict[str, Any]] = []

    async def capture_chat(frame: dict[str, Any]) -> None:
        chat_frames.append(frame)

    async def capture_user(frame: dict[str, Any]) -> None:
        user_frames.append(frame)

    hub.register("chat-conn", capture_chat)
    hub.subscribe("thr_1", "chat-conn")
    hub.register("user-conn", capture_user, user_id=7)

    row = _thread()
    gateway = object.__new__(Gateway)
    gateway._thread_registry = SimpleNamespace(get_thread=lambda _tid: row)  # type: ignore[assignment]
    gateway._ws_hub = hub  # type: ignore[assignment]
    gateway._repos = SimpleNamespace(  # type: ignore[assignment]
        session_repo=SimpleNamespace(unread_counts_for_threads=lambda _ids: {"thr_1": 3})
    )

    await gateway.publish_thread_updated(
        user_id=7,
        agent_id="a1",
        thread_id="thr_1",
        turn_id="turn_1",
        active=True,
    )

    assert hub.is_turn_active("thr_1")
    assert chat_frames == [
        {
            "type": "turn.started",
            "turn_id": "turn_1",
            "agent_id": "a1",
            "thread_id": "thr_1",
            "source": "im",
        }
    ]
    assert user_frames == [
        {
            "type": "thread.updated",
            "agent_id": "a1",
            "thread_id": "thr_1",
            "channel_type": "feishu",
            "turn_active": True,
            "unread_count": 3,
            "title": "Feishu chat",
            "last_active": 1,
        }
    ]

    await gateway.publish_thread_updated(
        user_id=7,
        agent_id="a1",
        thread_id="thr_1",
        turn_id="turn_1",
        active=False,
    )

    assert not hub.is_turn_active("thr_1")
    assert chat_frames[-1]["type"] == "turn.ended"
    assert user_frames[-1]["turn_active"] is False


@pytest.mark.asyncio
async def test_im_turn_waiting_detects_locked_session() -> None:
    lock = asyncio.Lock()
    gateway = object.__new__(Gateway)
    gateway._channel_manager = SimpleNamespace(  # type: ignore[assignment]
        _session_locks={"ch-feishu:ou_x": lock}
    )

    assert gateway.im_turn_waiting("ch-feishu", "ou_x") is False
    async with lock:
        assert gateway.im_turn_waiting("ch-feishu", "ou_x") is True
    assert gateway.im_turn_waiting("ch-feishu", "ou_other") is False


def test_cancel_im_bridge_turn_consumes_once() -> None:
    gateway = object.__new__(Gateway)
    gateway._cancelled_bridge_turns = {}
    gateway.cancel_im_bridge_turn("bridge_x")
    assert gateway.consume_im_bridge_cancel("bridge_x") is True
    assert gateway.consume_im_bridge_cancel("bridge_x") is False


@pytest.mark.asyncio
async def test_processor_skips_cancelled_bridge_turn() -> None:
    processor = object.__new__(GlobalProcessor)
    processor._gateway = SimpleNamespace(  # type: ignore[assignment]
        consume_im_bridge_cancel=lambda tid: tid == "bridge_x"
    )
    processor._agent_repo = SimpleNamespace(  # type: ignore[assignment]
        get=lambda _aid: SimpleNamespace(user_id=7)
    )
    msg = InboundMessage(
        channel_id="ch-feishu",
        channel_type="feishu",
        tenant_id="a1",
        channel_subject=ChannelSubject(subject_id="ou_x"),
        content=[TextContent(text="hi")],
        metadata={
            "session_key": "a1:feishu:ou_x:dm",
            "bridge_turn_id": "bridge_x",
        },
    )

    events = [event async for event in processor(msg)]

    assert len(events) == 1
    assert events[0].type == MessageEventType.COMPLETED


def test_im_turn_waiting_without_manager_is_false() -> None:
    gateway = object.__new__(Gateway)
    gateway._channel_manager = None  # type: ignore[assignment]
    assert gateway.im_turn_waiting("ch-feishu", "ou_x") is False


def test_enqueue_im_turn_reports_missing_channel() -> None:
    enqueued: list[tuple[str, Any]] = []
    gateway = object.__new__(Gateway)
    gateway._channel_manager = SimpleNamespace(  # type: ignore[assignment]
        get_channel=lambda cid: object() if cid == "ch-live" else None,
        enqueue=lambda cid, msg: enqueued.append((cid, msg)),
    )

    assert gateway.enqueue_im_turn("ch-live", {"text": "hi"}) is True
    assert enqueued == [("ch-live", {"text": "hi"})]
    assert gateway.enqueue_im_turn("ch-gone", {"text": "hi"}) is False


def test_channel_output_tracker_ignores_narration_before_tool() -> None:
    tracker = _ChannelOutputTracker()
    tracker.observe(MessageEvent.delta("let me look"))
    tracker.observe(MessageEvent(type=MessageEventType.TOOL_START))
    assert tracker.has_output is False

    tracker.observe(MessageEvent.text("here is the answer"))
    assert tracker.has_output is True


def _processor_for_touch(
    hub: WebSocketHub,
) -> tuple[GlobalProcessor, SimpleNamespace]:
    registry = SimpleNamespace(increment_unread=MagicMock(), touch_session=MagicMock())
    processor = object.__new__(GlobalProcessor)
    processor._gateway = SimpleNamespace(ws_hub=hub)  # type: ignore[assignment]
    processor._thread_registry = registry  # type: ignore[assignment]
    return processor, registry


def test_im_turn_marks_unread_when_user_is_not_viewing() -> None:
    processor, registry = _processor_for_touch(WebSocketHub())
    processor._touch_im_session_after_turn(
        channel_type="feishu",
        session_key="a1:feishu:ou_x:dm",
        thread_id="thr_1",
        origin="feishu",
    )
    registry.increment_unread.assert_called_once_with("a1:feishu:ou_x:dm")
    registry.touch_session.assert_not_called()


@pytest.mark.asyncio
async def test_im_turn_touches_session_when_viewing_or_web_origin() -> None:
    hub = WebSocketHub()

    async def capture(_frame: dict[str, Any]) -> None:
        return

    hub.register("conn", capture)
    hub.subscribe("thr_1", "conn")

    processor, registry = _processor_for_touch(hub)
    processor._touch_im_session_after_turn(
        channel_type="feishu",
        session_key="a1:feishu:ou_x:dm",
        thread_id="thr_1",
        origin="feishu",
    )
    registry.increment_unread.assert_not_called()
    registry.touch_session.assert_called_once_with("a1:feishu:ou_x:dm")

    processor, registry = _processor_for_touch(WebSocketHub())
    processor._touch_im_session_after_turn(
        channel_type="feishu",
        session_key="a1:feishu:ou_x:dm",
        thread_id="thr_1",
        origin="web",
    )
    registry.increment_unread.assert_not_called()
    registry.touch_session.assert_called_once_with("a1:feishu:ou_x:dm")


def test_dashboard_turn_does_not_touch_im_session() -> None:
    processor, registry = _processor_for_touch(WebSocketHub())
    processor._touch_im_session_after_turn(
        channel_type="dashboard",
        session_key="a1:dashboard:7:dm",
        thread_id="thr_1",
        origin="web",
    )
    registry.increment_unread.assert_not_called()
    registry.touch_session.assert_not_called()


def test_channel_output_tracker_treats_error_as_visible_output() -> None:
    tracker = _ChannelOutputTracker()
    tracker.observe(MessageEvent(type=MessageEventType.ERROR, error="provider failed"))
    assert tracker.has_output is True


def test_channel_output_tracker_keeps_media_output() -> None:
    tracker = _ChannelOutputTracker()
    tracker.observe(
        MessageEvent(
            type=MessageEventType.MESSAGE,
            content=[ImageContent(url="https://example.com/a.png")],
        )
    )
    assert tracker.has_output is True
