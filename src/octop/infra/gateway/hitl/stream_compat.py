"""Recover HITL interrupts dropped by octop-harness on LangGraph astream v2.

LangGraph v2 moves ``__interrupt__`` off the values payload onto
``chunk["interrupts"]``. Harness ``_project_chunk`` only reads
``data["__interrupt__"]`` on updates, so ``hitl_required`` never reaches
Octop and the dashboard leaves ``ask_user_question`` spinning.

LangGraph v2 publishes every interrupt twice — once on the ``updates``
chunk and once on the ``values`` chunk — and both copies carry the same
interrupt id. The recovered copy therefore has to be de-duplicated against
the one the harness already projected, or the user gets two approval cards
for one pause. The interrupt id is what the de-duplication keys on: the
same tool call can legitimately be interrupted again later (same payload,
new id), and that second pause must still reach the user.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from octop.infra.gateway.hitl.format import normalize_hitl_request

_INSTALLED = False

# Private marker carrying the originating LangGraph interrupt id. Stripped
# before the chunk leaves the harness protocol.
_INTERRUPT_ID_KEY = "_octop_interrupt_id"


def _interrupt_id(item: Any) -> Any:
    """LangGraph interrupt id of a raw interrupt item, or ``None``."""
    if isinstance(item, dict):
        return item.get("id")
    return getattr(item, "id", None)


def _interrupt_pairs(interrupts: Any) -> list[tuple[dict[str, Any], Any]]:
    """Project interrupt items into ``hitl_required`` events paired with their ids."""
    if not interrupts:
        return []
    if not isinstance(interrupts, (list, tuple)):
        interrupts = (interrupts,)
    out: list[tuple[dict[str, Any], Any]] = []
    for item in interrupts:
        value: Any = item
        if isinstance(item, dict):
            value = item.get("value", item)
        elif hasattr(item, "value"):
            value = item.value
        request = normalize_hitl_request(value)
        if not request.get("action_requests"):
            continue
        out.append(({"type": "hitl_required", "request": request}, _interrupt_id(item)))
    return out


def hitl_events_from_interrupts(interrupts: Any) -> list[dict[str, Any]]:
    """Project LangGraph interrupt items into harness ``hitl_required`` chunks."""
    return [event for event, _interrupt_id in _interrupt_pairs(interrupts)]


def _hitl_has_actions(event: dict[str, Any]) -> bool:
    if event.get("type") != "hitl_required":
        return False
    request = event.get("request")
    return isinstance(request, dict) and bool(request.get("action_requests"))


def merge_hitl_projection(
    events: list[dict[str, Any]],
    interrupts: Any,
) -> list[dict[str, Any]]:
    """Unwrap existing ``hitl_required`` events and fill from v2 interrupts if empty."""
    pairs = _interrupt_pairs(interrupts)
    for event in events:
        if event.get("type") != "hitl_required":
            continue
        request = event.get("request")
        if isinstance(request, dict):
            event["request"] = normalize_hitl_request(request)
    if pairs and not any(_hitl_has_actions(event) for event in events):
        events = [event for event in events if event.get("type") != "hitl_required"]
        for event, interrupt_id in pairs:
            if interrupt_id is not None:
                event[_INTERRUPT_ID_KEY] = interrupt_id
            events.append(event)
    return events


def _tag_hitl_events(events: list[dict[str, Any]], chunk: dict[str, Any]) -> None:
    """Tag harness-projected HITL events with the interrupt id they came from.

    The harness projects the ``updates`` chunk on its own and drops the id,
    so the id is recovered from the raw chunk. Events that already carry a
    tag (the ones this module recovered from the ``values`` chunk) are left
    alone.
    """
    untagged = [
        event
        for event in events
        if event.get("type") == "hitl_required" and _INTERRUPT_ID_KEY not in event
    ]
    if not untagged:
        return
    data = chunk.get("data")
    items = data.get("__interrupt__") if isinstance(data, dict) else None
    if not isinstance(items, (list, tuple)) or len(items) != len(untagged):
        return
    for event, item in zip(untagged, items, strict=True):
        interrupt_id = _interrupt_id(item)
        if interrupt_id is not None:
            event[_INTERRUPT_ID_KEY] = interrupt_id


async def _dedupe_hitl_events(
    source: AsyncIterator[dict[str, Any]],
) -> AsyncIterator[dict[str, Any]]:
    """Drop the second copy of an interrupt LangGraph published twice."""
    seen: set[str] = set()
    async for event in source:
        if isinstance(event, dict) and event.get("type") == "hitl_required":
            interrupt_id = event.pop(_INTERRUPT_ID_KEY, None)
            if interrupt_id is not None:
                if interrupt_id in seen:
                    continue
                seen.add(interrupt_id)
        yield event


def install_harness_hitl_compat() -> None:
    """Patch harness stream projection so v2 interrupts become ``hitl_required``."""
    global _INSTALLED
    if _INSTALLED:
        return
    from octop_harness.protocols.langgraph import LangGraphProtocol

    original = LangGraphProtocol._project_chunk
    original_stream = LangGraphProtocol.stream
    original_resume = LangGraphProtocol.resume_stream

    def _project_chunk(self: Any, chunk: Any, splitter: Any) -> list[dict[str, Any]]:
        events = original(self, chunk, splitter)
        if isinstance(chunk, dict):
            events = merge_hitl_projection(events, chunk.get("interrupts"))
            _tag_hitl_events(events, chunk)
        return events

    async def _stream(self: Any, *args: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        async for event in _dedupe_hitl_events(original_stream(self, *args, **kwargs)):
            yield event

    async def _resume_stream(self: Any, *args: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        async for event in _dedupe_hitl_events(original_resume(self, *args, **kwargs)):
            yield event

    LangGraphProtocol._project_chunk = _project_chunk  # type: ignore[method-assign]
    LangGraphProtocol.stream = _stream  # type: ignore[method-assign,assignment]
    LangGraphProtocol.resume_stream = _resume_stream  # type: ignore[method-assign,assignment]
    _INSTALLED = True


__all__ = [
    "hitl_events_from_interrupts",
    "install_harness_hitl_compat",
    "merge_hitl_projection",
]
