"""MiniApps referenced by one conversation (the chat dock's MiniApps tab).

The transcript is the record of which apps a conversation touched: every
``app_open`` / template-created app result carries an ``octop_ui`` envelope
with renderer ``app_card``. The dashboard only keeps the latest history page
in memory, so it asks the server to scan the whole thread instead.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

APP_CARD_RENDERER = "app_card"


def app_card_ids(messages: Iterable[Any]) -> list[str]:
    """App ids of every ``app_card`` in *messages*, newest mention first.

    Accepts stored LangChain wires (``{"data": {"content": ...}}``) and
    projected history entries (``{"content": [{"type": "tool_result", ...}]}``);
    only the envelope inside the content matters. A repeated mention refreshes
    the app's position instead of adding a second entry.
    """
    order: dict[str, None] = {}
    for message in messages:
        for app_id in _message_app_ids(message):
            order.pop(app_id, None)
            order[app_id] = None
    return list(reversed(order))


def _message_app_ids(message: Any) -> list[str]:
    if not isinstance(message, dict):
        return []
    role = message.get("role")
    content = message.get("content")
    data = message.get("data")
    if isinstance(data, dict):
        role = role or data.get("type")
        if "content" in data:
            content = data["content"]
    # A card is a tool result; the same JSON quoted in user/assistant prose is
    # not evidence that the conversation opened the app.
    if (role or message.get("type")) != "tool":
        return []
    return [
        app_id
        for candidate in _content_candidates(content)
        if (app_id := _card_app_id(candidate)) is not None
    ]


def _content_candidates(value: Any) -> Iterable[Any]:
    """Strings and already-parsed envelopes anywhere inside one message content."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        if isinstance(value.get("octop_ui"), dict):
            yield value
            return
        for key in ("text", "output", "content"):
            if key in value:
                yield from _content_candidates(value[key])
    elif isinstance(value, list):
        for item in value:
            yield from _content_candidates(item)


def _card_app_id(candidate: Any) -> str | None:
    if isinstance(candidate, dict):
        envelope = candidate
    else:
        # Cheap filter: the transcript also holds large tool outputs.
        if APP_CARD_RENDERER not in candidate:
            return None
        try:
            envelope = json.loads(candidate)
        except (TypeError, ValueError):
            return None
    if not isinstance(envelope, dict):
        return None
    hint = envelope.get("octop_ui")
    if not isinstance(hint, dict) or hint.get("renderer") != APP_CARD_RENDERER:
        return None
    data = envelope.get("data")
    if not isinstance(data, dict):
        return None
    app_id = data.get("app_id")
    return app_id if isinstance(app_id, str) and app_id else None


__all__ = ["APP_CARD_RENDERER", "app_card_ids"]
