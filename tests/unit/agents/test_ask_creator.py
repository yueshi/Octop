"""Unit tests for the ``ask_creator`` tool (team host escalation pause)."""

from __future__ import annotations

import json
from typing import Any

from octop.infra.agents.teams.ask_creator import (
    ASK_CREATOR_TOOL_NAME,
    AskCreatorArgs,
    build_ask_creator_tool,
)
from octop.infra.gateway.hitl.format import is_ask_action_requests


def test_tool_name_and_schema_shape() -> None:
    tool = build_ask_creator_tool()
    assert tool.name == ASK_CREATOR_TOOL_NAME
    args = AskCreatorArgs.model_validate(
        {
            "questions": [
                {
                    "question": "Buy the dataset or scrape it ourselves?",
                    "header": "Data",
                    "options": [{"label": "Buy", "description": "faster"}],
                }
            ],
            "task_id": " t9 ",
        }
    )
    assert args.task_id == "t9"
    assert args.questions[0].header == "Data"


def test_tool_body_is_headless_fallback() -> None:
    """Without an interrupt channel the body must never block (fail-safe)."""
    from octop_harness.builtin.tools.ask_user import HEADLESS_FALLBACK

    tool = build_ask_creator_tool()

    async def _invoke() -> str:
        return await tool.ainvoke(
            {
                "questions": [{"question": "A or B?"}],
                "task_id": None,
            }
        )

    import asyncio

    assert asyncio.run(_invoke()) == HEADLESS_FALLBACK


def test_schema_dump_matches_ask_card_shape() -> None:
    """The questions payload must parse through the shared ask-card extractors."""
    args = AskCreatorArgs.model_validate(
        {"questions": [{"question": "A or B?", "options": [{"label": "A"}]}]}
    )
    dumped: dict[str, Any] = json.loads(args.model_dump_json())
    assert dumped["questions"][0]["question"] == "A or B?"
    assert dumped["questions"][0]["options"][0]["label"] == "A"


def test_is_ask_action_requests_recognizes_ask_creator() -> None:
    assert is_ask_action_requests([{"name": ASK_CREATOR_TOOL_NAME, "args": {"task_id": "t1"}}])
    assert is_ask_action_requests([{"name": "ask_user_question", "args": {}}])
    assert not is_ask_action_requests([{"name": "execute", "args": {}}])
