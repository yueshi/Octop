"""Acting agent/user scope for tool calls that resume outside a chat turn.

A normal turn carries ``agent_id`` and ``user`` in LangGraph's ``configurable``.
A HITL resume starts a fresh graph invocation with only ``thread_id``, so tools
that authorize against the caller must fall back to this scope.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_CURRENT_TOOL_SCOPE: ContextVar[tuple[str, int] | None] = ContextVar(
    "octop_tool_scope",
    default=None,
)


@contextmanager
def tool_scope(agent_id: str, user_id: int | None) -> Iterator[None]:
    """Bind the acting identity for tools invoked during a HITL resume."""
    token = _CURRENT_TOOL_SCOPE.set((agent_id, user_id) if user_id is not None else None)
    try:
        yield
    finally:
        _CURRENT_TOOL_SCOPE.reset(token)


def current_tool_scope() -> tuple[str, int] | None:
    return _CURRENT_TOOL_SCOPE.get()


__all__ = ["current_tool_scope", "tool_scope"]
