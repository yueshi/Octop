"""Unit tests for the chat dock's whole-thread MiniApp scan."""

from __future__ import annotations

import json
from typing import Any

from octop.infra.apps.session import app_card_ids


def _card(app_id: str, *, name: str = "") -> str:
    return json.dumps(
        {
            "octop_ui": {"renderer": "app_card", "version": 1},
            "data": {"app_id": app_id, "name": name or app_id},
        },
        ensure_ascii=False,
    )


def _wire(content: Any) -> dict[str, Any]:
    return {"type": "tool", "data": {"content": content, "name": "app_open"}}


def _entry(content: Any) -> dict[str, Any]:
    return {"role": "tool", "content": content}


def test_reads_stored_wires_and_projected_entries() -> None:
    assert app_card_ids(
        [
            _wire(_card("app_a")),
            _entry([{"type": "tool_result", "output": _card("app_b")}]),
        ]
    ) == ["app_b", "app_a"]


def test_ignores_other_renderers_and_plain_output() -> None:
    other = json.dumps({"octop_ui": {"renderer": "wiki_summary_card"}, "data": {"app_id": "app_x"}})
    assert (
        app_card_ids(
            [
                _wire(other),
                _wire("not json but mentions app_card"),
                _wire(json.dumps({"app_id": "app_y"})),
                {"role": "tool", "content": [{"type": "text", "text": "hello"}]},
                {"type": "human", "data": {"content": _card("app_quoted")}},
                {"role": "assistant", "content": [{"type": "text", "text": _card("app_prose")}]},
                "not-a-message",
            ]
        )
        == []
    )


def test_handles_envelope_objects_and_nested_blocks() -> None:
    envelope = {
        "octop_ui": {"renderer": "app_card", "version": 1},
        "data": {"app_id": "app_obj"},
    }
    assert app_card_ids(
        [
            {"role": "tool", "content": [envelope]},
            _entry([{"type": "tool_result", "output": [{"text": _card("app_str")}]}]),
        ]
    ) == ["app_str", "app_obj"]


def test_repeated_mentions_refresh_position_without_duplicates() -> None:
    assert app_card_ids(
        [
            _wire(_card("app_a")),
            _wire(_card("app_b")),
            _wire(_card("app_a")),
        ]
    ) == ["app_a", "app_b"]


def test_empty_ids_are_skipped() -> None:
    assert app_card_ids(
        [
            _wire(json.dumps({"octop_ui": {"renderer": "app_card"}, "data": {"app_id": ""}})),
            _wire(json.dumps({"octop_ui": {"renderer": "app_card"}, "data": {}})),
            _wire(_card("app_ok")),
        ]
    ) == ["app_ok"]
