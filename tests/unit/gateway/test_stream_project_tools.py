"""tests/unit/gateway/test_stream_project_tools.py"""

from __future__ import annotations

import pytest

from octop.infra.gateway.process.stream_project import enrich_tool_stream_chunk


def test_enrich_tool_stream_chunk_adds_display_name():
    chunk = {"type": "tool_call_chunk", "name": "read_file", "args": "{"}
    out = enrich_tool_stream_chunk(chunk, "zh")
    assert out["display_name"] == "读取文件"
    assert out["name"] == "read_file"


def test_enrich_tool_stream_chunk_ignores_non_tool():
    chunk = {"type": "token", "content": "hi"}
    assert enrich_tool_stream_chunk(chunk, "zh") is chunk


def test_tool_start_event_carries_localized_hint():
    from octop_gateway.models import MessageEvent

    from octop.i18n import channel_tool_hint_start

    label = "读取文件"
    event = MessageEvent.tool_start(
        label,
        tool_hint_text=channel_tool_hint_start(label, "zh"),
    )
    assert event.metadata["tool_hint_text"] == channel_tool_hint_start(label, "zh")
    assert "正在调用工具" in event.metadata["tool_hint_text"]


@pytest.mark.asyncio
async def test_project_stream_skips_replayed_historical_media(tmp_path) -> None:
    """A full-history tool_result must not re-send media from earlier turns."""
    import base64
    from types import SimpleNamespace

    from octop_gateway.models import ImageContent
    from tests.support.fakes import FakeHarnessAgent

    from octop.infra.gateway.media.backend_files import media_preview_url
    from octop.infra.gateway.process.stream_project import project_stream

    agent = FakeHarnessAgent(workspace_dir=tmp_path)
    outbound = tmp_path / "outbound"
    outbound.mkdir()
    live_png = outbound / "live.png"
    live_png.write_bytes(b"LIVE")
    old_png = outbound / "old.png"
    old_png.write_bytes(b"OLD")

    old_preview = media_preview_url("main", old_png.as_uri(), "image/png")
    old_message = {
        "tool_call_id": "call-old",
        "name": "send_file_to_user",
        "content": {
            "type": "image",
            "source": {"type": "url", "url": old_preview, "media_type": "image/png"},
            "preview_url": old_preview,
            "filename": "old.png",
        },
    }
    live_message = {
        "tool_call_id": "call-live",
        "name": "send_file_to_user",
        "content": {
            "type": "image",
            "source": {"type": "url", "url": live_png.as_uri(), "media_type": "image/png"},
            "filename": "live.png",
        },
    }

    async def _stream(*_args: object, **_kwargs: object):
        yield {
            "type": "tool_call_chunk",
            "id": "call-live",
            "name": "browser_use",
            "args": "{}",
            "index": 0,
        }
        yield {"type": "tool_result", "messages": [old_message, live_message]}

    manager = SimpleNamespace(get_agent=lambda _agent_id: agent, stream=_stream)
    events = [event async for event in project_stream(manager, "main", {"thread_id": "thr1"})]
    parts = [part for event in events for part in event.content if isinstance(part, ImageContent)]

    assert len(parts) == 1
    assert parts[0].url == ""
    assert base64.b64decode(parts[0].data) == b"LIVE"


@pytest.mark.asyncio
async def test_project_stream_stops_after_time_budget(tmp_path) -> None:
    from types import SimpleNamespace

    from tests.support.fakes import FakeHarnessAgent

    from octop.infra.gateway.process.stream_project import StreamBudget, project_stream

    agent = FakeHarnessAgent(workspace_dir=tmp_path)
    cancelled: list[tuple[str, str]] = []

    async def _stream(*_args: object, **_kwargs: object):
        yield {"type": "token", "content": "working"}

    manager = SimpleNamespace(
        get_agent=lambda _agent_id: agent,
        stream=_stream,
        cancel_stream=lambda agent_id, thread_id: cancelled.append((agent_id, thread_id)),
    )
    events = [
        event
        async for event in project_stream(
            manager,
            "main",
            {"thread_id": "thr1"},
            locale="en",
            budget=StreamBudget(max_seconds=0.0),
        )
    ]
    texts = [part.text for event in events for part in event.content if hasattr(part, "text")]

    assert any("stopped to avoid making you wait" in text for text in texts)
    assert cancelled == [("main", "thr1")]


@pytest.mark.asyncio
async def test_project_stream_stops_on_repeated_tool_errors(tmp_path) -> None:
    from types import SimpleNamespace

    from tests.support.fakes import FakeHarnessAgent

    from octop.infra.gateway.process.stream_project import StreamBudget, project_stream

    agent = FakeHarnessAgent(workspace_dir=tmp_path)
    cancelled: list[tuple[str, str]] = []
    chunks: list[dict[str, object]] = []
    for index in range(1, 4):
        chunks.append(
            {
                "type": "tool_call_chunk",
                "id": f"call-{index}",
                "name": "execute",
                "args": "{}",
                "index": index,
            }
        )
        chunks.append(
            {
                "type": "tool_result",
                "messages": [
                    {
                        "tool_call_id": f"call-{index}",
                        "name": "execute",
                        "content": "Error: timed out",
                        "status": "error",
                    }
                ],
            }
        )

    async def _stream(*_args: object, **_kwargs: object):
        for chunk in chunks:
            yield chunk

    manager = SimpleNamespace(
        get_agent=lambda _agent_id: agent,
        stream=_stream,
        cancel_stream=lambda agent_id, thread_id: cancelled.append((agent_id, thread_id)),
    )
    events = [
        event
        async for event in project_stream(
            manager,
            "main",
            {"thread_id": "thr1"},
            locale="en",
            budget=StreamBudget(max_tool_calls=10, max_seconds=999, max_repeats=3),
        )
    ]
    texts = [part.text for event in events for part in event.content if hasattr(part, "text")]

    assert any("same tool step keeps failing" in text for text in texts)
    assert cancelled == [("main", "thr1")]
