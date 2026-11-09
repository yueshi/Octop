"""Unit tests for one-shot light-app text generation."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from octop.i18n import tr
from octop.infra.apps.generation import (
    MAX_OUTPUT_CHARS,
    AppGenerator,
)
from octop.infra.errors import ErrorCode, OctopError
from tests.unit.apps.test_service import World, _create, world  # noqa: F401


class _FakeModel:
    """Returns one scripted result, or raises it when it is an exception."""

    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[list[Any]] = []

    async def ainvoke(self, messages: list[Any]) -> Any:
        self.calls.append(list(messages))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


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


def _generator(
    w: World,
    model: _FakeModel,
    usage: _UsageRepo | None = None,
    agents: _Agents | None = None,
) -> AppGenerator:
    return AppGenerator(
        agents=agents or _Agents(model),
        usage_repo=usage,
    )


def _final(text: str) -> AIMessage:
    return AIMessage(
        content=text,
        usage_metadata={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
    )


async def test_generate_returns_text_and_records_usage(world: World) -> None:  # noqa: F811
    app_id = await _create(world)
    row = world.service.get(app_id)
    usage = _UsageRepo()
    model = _FakeModel(_final("  A short summary.  "))
    generated = await _generator(world, model, usage).generate(
        row, user_id=world.alice_id, prompt="  summarize this  "
    )

    assert generated.text == "A short summary."
    assert generated.model_ref == "test-model"
    assert (generated.input_tokens, generated.output_tokens) == (10, 4)

    system, human = model.calls[0]
    assert isinstance(system, SystemMessage)
    assert isinstance(human, HumanMessage)
    assert "Tracker" in str(system.content)
    assert human.content == "summarize this"

    assert len(usage.rows) == 1
    assert usage.rows[0]["source"] == "app_generate"
    assert usage.rows[0]["model"] == "test-model"
    assert usage.rows[0]["agent_id"] == row.agent_id
    assert usage.rows[0]["user_id"] == world.alice_id


async def test_generate_unwraps_model_access(world: World) -> None:  # noqa: F811
    app_id = await _create(world)
    row = world.service.get(app_id)
    model = _FakeModel(_final("ok"))
    generated = await _generator(world, model, agents=_AccessAgents(model)).generate(
        row, user_id=world.alice_id, prompt="hi"
    )
    assert generated.text == "ok"


async def test_generate_appends_app_system_prompt(world: World) -> None:  # noqa: F811
    app_id = await _create(world)
    row = world.service.get(app_id)
    model = _FakeModel(_final("ok"))
    await _generator(world, model).generate(
        row,
        user_id=world.alice_id,
        prompt="name these rows",
        system="Answer in JSON.",
    )
    system = model.calls[0][0]
    assert str(system.content).startswith(
        tr("light_apps.generate_system_prompt", "zh", app_name="Tracker")
    )
    assert str(system.content).endswith("Answer in JSON.")


async def test_generate_strips_thinking_tags(world: World) -> None:  # noqa: F811
    app_id = await _create(world)
    row = world.service.get(app_id)
    model = _FakeModel(_final("<think>hidden</think>The answer"))
    generated = await _generator(world, model).generate(row, user_id=world.alice_id, prompt="hi")
    assert generated.text == "The answer"


async def test_generate_rejects_blank_prompt(world: World) -> None:  # noqa: F811
    app_id = await _create(world)
    row = world.service.get(app_id)
    usage = _UsageRepo()
    model = _FakeModel(_final("unused"))
    with pytest.raises(OctopError) as exc:
        await _generator(world, model, usage).generate(row, user_id=world.alice_id, prompt="   ")
    assert exc.value.code == ErrorCode.APP_PROMPT_INVALID
    assert model.calls == []
    assert usage.rows == []


async def test_generate_reports_model_failure(world: World) -> None:  # noqa: F811
    app_id = await _create(world)
    row = world.service.get(app_id)
    usage = _UsageRepo()
    model = _FakeModel(RuntimeError("boom"))
    with pytest.raises(OctopError) as exc:
        await _generator(world, model, usage).generate(row, user_id=world.alice_id, prompt="hi")
    assert exc.value.code == ErrorCode.APP_GENERATE_FAILED
    assert usage.rows == []


async def test_generate_truncates_long_output(world: World) -> None:  # noqa: F811
    app_id = await _create(world)
    row = world.service.get(app_id)
    model = _FakeModel(_final("x" * (MAX_OUTPUT_CHARS + 100)))
    generated = await _generator(world, model).generate(row, user_id=world.alice_id, prompt="hi")
    assert len(generated.text) == MAX_OUTPUT_CHARS
