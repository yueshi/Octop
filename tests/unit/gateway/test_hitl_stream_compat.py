"""LangGraph v2 interrupt projection for dashboard HITL."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from octop.infra.gateway.hitl.stream_compat import (
    _INTERRUPT_ID_KEY,
    _dedupe_hitl_events,
    _tag_hitl_events,
    hitl_events_from_interrupts,
    merge_hitl_projection,
)


class _FakeInterrupt:
    """Stand-in for LangGraph's ``Interrupt`` (carries ``id`` and ``value``)."""

    def __init__(self, id_: str) -> None:
        self.id = id_
        self.value = {"action_requests": [{"name": "execute", "args": {"command": "ls"}}]}


async def _aiter(events: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    for event in events:
        yield event


def test_hitl_events_from_interrupt_objects() -> None:
    class _Interrupt:
        def __init__(self, value: object) -> None:
            self.value = value

    events = hitl_events_from_interrupts(
        (
            _Interrupt(
                {
                    "action_requests": [
                        {
                            "name": "ask_user_question",
                            "args": {"questions": [{"question": "Go?"}]},
                        }
                    ]
                }
            ),
        )
    )
    assert len(events) == 1
    assert events[0]["type"] == "hitl_required"
    assert events[0]["request"]["action_requests"][0]["name"] == "ask_user_question"


def test_hitl_events_from_interrupt_dicts() -> None:
    events = hitl_events_from_interrupts(
        [
            {
                "id": "abc",
                "value": {
                    "action_requests": [{"name": "execute", "args": {"command": "ls"}}],
                },
            }
        ]
    )
    assert events[0]["request"]["action_requests"][0]["args"]["command"] == "ls"


def test_hitl_events_ignore_empty_envelopes() -> None:
    assert hitl_events_from_interrupts([{"id": "x", "value": {"raw": "nope"}}]) == []
    assert hitl_events_from_interrupts(None) == []


def test_merge_replaces_empty_hitl_required_from_interrupts() -> None:
    events = merge_hitl_projection(
        [{"type": "hitl_required", "request": {"id": "int-1", "value": {}}}],
        [
            {
                "id": "int-1",
                "value": {
                    "action_requests": [
                        {"name": "ask_user_question", "args": {"questions": [{"question": "Go?"}]}}
                    ]
                },
            }
        ],
    )
    assert len(events) == 1
    assert events[0]["request"]["action_requests"][0]["name"] == "ask_user_question"


def test_merge_keeps_actionable_hitl_required() -> None:
    existing = {
        "type": "hitl_required",
        "request": {"action_requests": [{"name": "execute", "args": {"command": "ls"}}]},
    }
    events = merge_hitl_projection(
        [existing],
        [{"value": {"action_requests": [{"name": "ask_user_question", "args": {}}]}}],
    )
    assert events == [existing]


def test_merge_tags_recovered_event_with_interrupt_id() -> None:
    events = merge_hitl_projection(
        [],
        [
            {
                "id": "int-7",
                "value": {"action_requests": [{"name": "execute", "args": {"command": "ls"}}]},
            }
        ],
    )
    assert len(events) == 1
    assert events[0][_INTERRUPT_ID_KEY] == "int-7"


def test_tag_hitl_events_pairs_updates_chunk_interrupts() -> None:
    events = [
        {
            "type": "hitl_required",
            "request": {"action_requests": [{"name": "execute", "args": {"command": "ls"}}]},
        }
    ]
    chunk = {
        "type": "updates",
        "data": {"__interrupt__": (_FakeInterrupt("int-9"),)},
    }
    _tag_hitl_events(events, chunk)
    assert events[0][_INTERRUPT_ID_KEY] == "int-9"


def test_tag_hitl_events_ignores_unrelated_chunks() -> None:
    events = [
        {
            "type": "hitl_required",
            "request": {"action_requests": [{"name": "execute", "args": {"command": "ls"}}]},
        }
    ]
    _tag_hitl_events(events, {"type": "values", "data": {"messages": []}})
    assert _INTERRUPT_ID_KEY not in events[0]


async def test_dedupe_drops_repeated_interrupt() -> None:
    events = [
        {
            "type": "hitl_required",
            "request": {"action_requests": [{"name": "execute", "args": {"command": "ls"}}]},
            _INTERRUPT_ID_KEY: "int-1",
        },
        {
            "type": "hitl_required",
            "request": {"action_requests": [{"name": "execute", "args": {"command": "ls"}}]},
            _INTERRUPT_ID_KEY: "int-1",
        },
    ]
    kept = [event async for event in _dedupe_hitl_events(_aiter(events))]
    assert len(kept) == 1
    # The private marker never reaches the transport.
    assert _INTERRUPT_ID_KEY not in kept[0]


async def test_dedupe_keeps_distinct_interrupts() -> None:
    events = [
        {
            "type": "hitl_required",
            "request": {"action_requests": [{"name": "execute", "args": {"command": "ls"}}]},
            _INTERRUPT_ID_KEY: "int-1",
        },
        {
            "type": "hitl_required",
            "request": {"action_requests": [{"name": "execute", "args": {"command": "pwd"}}]},
            _INTERRUPT_ID_KEY: "int-2",
        },
    ]
    kept = [event async for event in _dedupe_hitl_events(_aiter(events))]
    assert len(kept) == 2


async def test_dedupe_keeps_events_without_an_interrupt_id() -> None:
    events = [
        {
            "type": "hitl_required",
            "request": {"action_requests": [{"name": "execute", "args": {"command": "ls"}}]},
        }
    ]
    kept = [event async for event in _dedupe_hitl_events(_aiter(events))]
    assert len(kept) == 1


async def test_dedupe_passes_other_events_through() -> None:
    events = [{"type": "token", "content": "hi"}]
    kept = [event async for event in _dedupe_hitl_events(_aiter(events))]
    assert kept == events
