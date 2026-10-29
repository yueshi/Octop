"""``ask_creator`` — team host escalates a real decision to its creator.

Semantically a wrapper over the ``ask_user_question`` HITL channel (the
middleware intercepts the call *before* execution and the creator answers via
``respond``), plus a ``task_id`` link so the pending pause and the
``approval_required`` notification carry board context. The questions payload
reuses the harness ``AskQuestion`` shape byte-for-byte so dashboard ask-cards
render without a new component; only the tool-name set grows.

The function body is the headless fail-safe: reached only when no interrupt
channel is attached — it must never block, mirroring ``ask_user_question``.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool
from octop_harness.builtin.tools.ask_user import (
    HEADLESS_FALLBACK,
    AskQuestion,
)
from pydantic import BaseModel, Field, field_validator

ASK_CREATOR_TOOL_NAME = "ask_creator"


class AskCreatorArgs(BaseModel):
    """Arguments for ``ask_creator``."""

    questions: list[AskQuestion] = Field(
        ...,
        min_length=1,
        max_length=4,
        description="Everything the creator must decide, asked in ONE call.",
    )
    task_id: str | None = Field(
        None,
        description="Team task this question blocks, when there is one.",
    )

    @field_validator("task_id")
    @classmethod
    def _strip_task_id(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value


async def _ask_creator(questions: list[Any], task_id: str | None = None) -> str:
    del questions, task_id  # The creator answers on behalf of this tool.
    return HEADLESS_FALLBACK


def build_ask_creator_tool() -> StructuredTool:
    return StructuredTool.from_function(
        coroutine=_ask_creator,
        name=ASK_CREATOR_TOOL_NAME,
        description=(
            "Ask the team creator to decide, then wait for their answer. Use "
            "only for forks the host cannot resolve alone: conflicting goals, "
            "destructive or costly actions, or a member blocked beyond its "
            "authority. Bundle everything into one call."
        ),
        args_schema=AskCreatorArgs,
    )


__all__ = [
    "ASK_CREATOR_TOOL_NAME",
    "AskCreatorArgs",
    "build_ask_creator_tool",
]
