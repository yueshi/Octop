"""Unit tests for the light-app data assistant."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from octop.i18n import tr
from octop.infra.apps.assistant import MAX_ASSISTANT_ROUNDS, AppAssistant
from octop.infra.db.repos.apps import (
    ASSISTANT_HISTORY_KEEP,
    ASSISTANT_ROLE_ASSISTANT,
    ASSISTANT_ROLE_USER,
)
from octop.infra.errors import ErrorCode, OctopError
from tests.unit.apps.test_service import World, _create, world  # noqa: F401


class _FakeModel:
    """Scripted LLM: pops one AIMessage per ainvoke call."""

    def __init__(self, script: list[AIMessage]) -> None:
        self._script = list(script)
        self.calls: list[list[Any]] = []

    def bind_tools(self, _tools: Any) -> _FakeModel:
        return self

    async def ainvoke(self, messages: list[Any]) -> AIMessage:
        self.calls.append(list(messages))
        return self._script.pop(0)


class _Agents:
    def __init__(self, model: _FakeModel) -> None:
        self.model = model

    def get_agent(self, agent_id: str) -> Any:
        _ = agent_id
        return SimpleNamespace(
            config=SimpleNamespace(pick_default_model_ref=lambda: "test-model"),
            model_factory=SimpleNamespace(get=lambda ref: self.model),
        )


class _AccessAgents(_Agents):
    """Factory returns a ModelAccess-style wrapper (production shape)."""

    def get_agent(self, agent_id: str) -> Any:
        base = super().get_agent(agent_id)
        return SimpleNamespace(
            config=base.config,
            model_factory=SimpleNamespace(get=lambda ref: SimpleNamespace(chat_model=self.model)),
        )


class _UsageRepo:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def record(self, **kwargs: Any) -> int:
        self.rows.append(kwargs)
        return len(self.rows)


def _assistant(
    w: World,
    model: _FakeModel,
    usage: _UsageRepo | None = None,
    agents: _Agents | None = None,
) -> AppAssistant:
    return AppAssistant(
        service=w.service,
        app_repo=w.repo,
        agents=agents or _Agents(model),
        usage_repo=usage,
    )


async def _collect(stream: AsyncIterator[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event async for event in stream]


def _final(text: str) -> AIMessage:
    return AIMessage(
        content=text,
        usage_metadata={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
    )


async def test_history_round_trip_and_trim(world: World) -> None:  # noqa: F811
    app_id = await _create(world)
    row = world.service.get(app_id)
    assistant = _assistant(world, _FakeModel([]))
    added = ASSISTANT_HISTORY_KEEP + 3
    for i in range(added):
        world.repo.add_assistant_message(
            app_id, world.alice_id, role=ASSISTANT_ROLE_USER, content=f"q{i}"
        )
        world.repo.add_assistant_message(
            app_id, world.alice_id, role=ASSISTANT_ROLE_ASSISTANT, content=f"a{i}"
        )
    # The repo keeps the most recent window per (app, user); the assistant's
    # history() is a 50-message page over it.
    kept = world.repo.list_assistant_messages(app_id, world.alice_id)
    assert len(kept) == ASSISTANT_HISTORY_KEEP
    first_kept = (2 * added - ASSISTANT_HISTORY_KEEP) // 2
    assert kept[0].content == f"q{first_kept}"  # oldest kept, oldest-first order
    turns = assistant.history(row, world.alice_id)
    assert len(turns) == 50
    assert turns[0].content == f"q{added - 25}"  # latest 50 messages = 25 rounds
    assert [t.role for t in turns[-2:]] == [ASSISTANT_ROLE_USER, ASSISTANT_ROLE_ASSISTANT]


async def test_prepare_strips_question_and_resolves_model(world: World) -> None:  # noqa: F811
    app_id = await _create(world)
    row = world.service.get(app_id)
    model = _FakeModel([])
    prepared = _assistant(world, model).prepare(
        row, user_id=world.alice_id, question="  hi there  "
    )
    assert prepared.question == "hi there"
    assert prepared.llm is model
    assert prepared.model_ref == "test-model"


async def test_prepare_unwraps_model_access(world: World) -> None:  # noqa: F811
    """The harness factory wraps the chat model; the loop needs the raw model."""
    app_id = await _create(world)
    row = world.service.get(app_id)
    model = _FakeModel([])
    prepared = _assistant(world, model, agents=_AccessAgents(model)).prepare(
        row, user_id=world.alice_id, question="hi"
    )
    assert prepared.llm is model


async def test_stream_direct_answer_records_history_and_usage(world: World) -> None:  # noqa: F811
    app_id = await _create(world)
    row = world.service.get(app_id)
    usage = _UsageRepo()
    model = _FakeModel([_final("42 units")])
    assistant = _assistant(world, model, usage)
    prepared = assistant.prepare(row, user_id=world.alice_id, question="how many?")
    events = await _collect(assistant.stream(prepared))

    assert [event["type"] for event in events] == ["token", "done"]
    assert events[0]["text"] == "42 units"
    assert events[1]["input_tokens"] == 10
    assert events[1]["output_tokens"] == 4

    turns = assistant.history(row, world.alice_id)
    assert [(t.role, t.content) for t in turns] == [
        (ASSISTANT_ROLE_USER, "how many?"),
        (ASSISTANT_ROLE_ASSISTANT, "42 units"),
    ]
    assert len(usage.rows) == 1
    assert usage.rows[0]["source"] == "app_assistant"
    assert usage.rows[0]["model"] == "test-model"
    assert usage.rows[0]["agent_id"] == row.agent_id


async def test_stream_tool_loop_queries_rows(world: World) -> None:  # noqa: F811
    app_id = await _create(world)
    row = world.service.get(app_id)
    await world.service.append_row(row, {"title": "Cup", "status": "new"})
    tool_call = {
        "name": "app_query_rows",
        "args": {"table": "main", "filters": {"title": "cup"}},
        "id": "c1",
        "type": "tool_call",
    }
    model = _FakeModel([AIMessage(content="", tool_calls=[tool_call]), _final("1 row: Cup")])
    assistant = _assistant(world, model)
    prepared = assistant.prepare(row, user_id=world.alice_id, question="find cup")
    events = await _collect(assistant.stream(prepared))

    assert [event["type"] for event in events] == ["tool", "token", "done"]
    assert events[0]["name"] == "app_query_rows"
    assert events[-1]["rounds"] == 2
    # The tool result fed back to the model contained the filtered row.
    tool_messages = [m for m in model.calls[1] if m.type == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0].tool_call_id == "c1"
    assert "Cup" in tool_messages[0].content


async def test_stream_chart_tool_emits_chart_and_persists(world: World) -> None:  # noqa: F811
    app_id = await _create(world)
    row = world.service.get(app_id)
    await world.service.append_row(row, {"title": "Cup", "qty": 2, "status": "new"})
    await world.service.append_row(row, {"title": "Pen", "qty": 3, "status": "new"})
    await world.service.append_row(row, {"title": "Book", "qty": 1, "status": "done"})
    tool_call = {
        "name": "app_chart",
        "args": {
            "chart_type": "bar",
            "x": "status",
            "y": "qty",
            "agg": "sum",
        },
        "id": "chart-1",
        "type": "tool_call",
    }
    model = _FakeModel([AIMessage(content="", tool_calls=[tool_call]), _final("Chart ready")])
    assistant = _assistant(world, model)
    prepared = assistant.prepare(row, user_id=world.alice_id, question="chart it")
    events = await _collect(assistant.stream(prepared))

    assert [event["type"] for event in events] == [
        "tool",
        "chart",
        "token",
        "done",
    ]
    chart = events[1]["chart"]
    assert chart["type"] == "bar"
    assert chart["series"] == [{"key": "qty", "label": "qty"}]
    assert chart["data"] == [
        {"x": "new", "qty": 5.0},
        {"x": "done", "qty": 1.0},
    ]
    assert events[-1]["charts"] == [chart]

    turns = assistant.history(row, world.alice_id)
    assert turns[-1].content == "Chart ready"
    assert turns[-1].charts == (chart,)

    # The marker must never leak back into the model context.
    follow_up_model = _FakeModel([_final("again")])
    follow_up = _assistant(world, follow_up_model)
    follow_up_prepared = follow_up.prepare(row, user_id=world.alice_id, question="and now?")
    await _collect(follow_up.stream(follow_up_prepared))
    assert all(
        "octop-charts:v1" not in str(message.content) for message in follow_up_model.calls[-1]
    )


async def test_stream_finalizes_after_tool_round_limit(world: World) -> None:  # noqa: F811
    app_id = await _create(world)
    row = world.service.get(app_id)
    call = {"name": "app_tables", "args": {}, "id": "c1", "type": "tool_call"}
    model = _FakeModel(
        [AIMessage(content="", tool_calls=[call]) for _ in range(MAX_ASSISTANT_ROUNDS)]
        + [_final("Based on the data already returned.")]
    )
    assistant = _assistant(world, model)
    prepared = assistant.prepare(row, user_id=world.alice_id, question="loop forever")
    events = await _collect(assistant.stream(prepared))

    assert events[-2]["type"] == "token"
    assert events[-1]["type"] == "done"
    assert events[-1]["rounds"] == MAX_ASSISTANT_ROUNDS
    turns = assistant.history(row, world.alice_id)
    assert turns[-1].content == "Based on the data already returned."


async def test_stream_reports_round_limit_without_final_answer(world: World) -> None:  # noqa: F811
    app_id = await _create(world)
    row = world.service.get(app_id)
    call = {"name": "app_tables", "args": {}, "id": "c1", "type": "tool_call"}
    model = _FakeModel(
        [AIMessage(content="", tool_calls=[call]) for _ in range(MAX_ASSISTANT_ROUNDS)]
        + [_final("")]
    )
    assistant = _assistant(world, model)
    prepared = assistant.prepare(row, user_id=world.alice_id, question="loop forever")
    events = await _collect(assistant.stream(prepared))

    assert events[-1]["type"] == "error"
    assert events[-1]["message"] == tr("light_apps.assistant_round_limit", "zh")
    assert assistant.history(row, world.alice_id) == []


async def test_stream_reports_model_failure(world: World) -> None:  # noqa: F811
    app_id = await _create(world)
    row = world.service.get(app_id)

    class _Broken:
        def bind_tools(self, _tools: Any) -> _Broken:
            return self

        async def ainvoke(self, messages: list[Any]) -> AIMessage:
            _ = messages
            raise RuntimeError("boom")

    agents = _Agents(
        SimpleNamespace(
            config=SimpleNamespace(pick_default_model_ref=lambda: "test-model"),
            model_factory=SimpleNamespace(get=lambda ref: _Broken()),
        )
    )
    assistant = AppAssistant(
        service=world.service, app_repo=world.repo, agents=agents, usage_repo=None
    )
    prepared = assistant.prepare(row, user_id=world.alice_id, question="hi")
    events = await _collect(assistant.stream(prepared))

    assert events == [{"type": "error", "message": tr("light_apps.assistant_failed", "zh")}]
    assert assistant.history(row, world.alice_id) == []


async def test_query_rows_filters_and_paging(world: World) -> None:  # noqa: F811
    app_id = await _create(world)
    row = world.service.get(app_id)
    await world.service.append_row(row, {"title": "Cup", "qty": 3, "status": "new"})
    await world.service.append_row(row, {"title": "Pen", "qty": 1, "status": "done"})

    _, rows, total = await world.service.query_rows(row, filters={"title": "cup"})
    assert total == 1
    assert rows[0]["title"] == "Cup"

    _, rows, total = await world.service.query_rows(row, filters={"status": "done"})
    assert total == 1
    assert rows[0]["title"] == "Pen"

    _, page, total = await world.service.query_rows(row, limit=1, offset=1)
    assert (total, len(page)) == (2, 1)

    with pytest.raises(OctopError) as exc:
        await world.service.query_rows(row, filters={"nope": "x"})
    assert exc.value.code == ErrorCode.APP_SCHEMA_INVALID


async def test_delete_clears_assistant_history(world: World) -> None:  # noqa: F811
    app_id = await _create(world)
    row = world.service.get(app_id)
    world.repo.add_assistant_message(app_id, world.alice_id, role=ASSISTANT_ROLE_USER, content="q")
    assert world.repo.list_assistant_messages(app_id, world.alice_id)
    await world.service.delete(row)
    assert world.repo.list_assistant_messages(app_id, world.alice_id) == []
