"""Self-execute: a digital employee runs the turn instead of dispatching it.

Harness refuses a peer call to oneself ("cannot collaborate with self"), so a
solo team cannot reuse ``submit_peer``. The autopilot routes `assignee ==
team_id` through its own stream call and reports the result as a member reply,
which is what settles the dispatch wait.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from octop.infra.agents.teams.dispatcher import DispatchTarget
from octop.infra.agents.teams.runtime import TeamAutopilot

TARGET = DispatchTarget(
    team_id="solo",
    user_id=1,
    thread_id="thr-1",
    session_key="solo:dashboard:1:dm",
)


class _Recorder:
    def __init__(self) -> None:
        self.replies: list[dict[str, Any]] = []
        self.handled: list[Any] = []

    def record_member_reply(self, **kwargs: Any) -> str:
        self.replies.append(kwargs)
        return "evt-1"

    def handle_member_reply(self, event: Any) -> None:
        self.handled.append(event)


class _AgentManager:
    def __init__(self, chunks: list[dict[str, Any]]) -> None:
        self._chunks = chunks
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def stream(self, agent_id: str, request: dict[str, Any]):
        self.calls.append((agent_id, request))
        for chunk in self._chunks:
            yield chunk


def _autopilot(agent_manager: Any, recorder: _Recorder) -> TeamAutopilot:
    autopilot = TeamAutopilot(
        repos=SimpleNamespace(),
        agent_manager=agent_manager,
        thread_registry=SimpleNamespace(),
    )
    autopilot.bind_coordinator(recorder)
    return autopilot


@pytest.mark.asyncio
async def test_self_assignee_runs_instead_of_dispatching() -> None:
    """No harness peer call: the agent runs its own turn."""
    recorder = _Recorder()
    manager = _AgentManager([{"type": "token", "content": "done it"}])
    autopilot = _autopilot(manager, recorder)

    job_id = await autopilot.submit_peer("solo", "solo", "do the thing", TARGET)
    await asyncio.sleep(0.05)  # the turn runs in a background task

    assert job_id is not None and job_id.startswith("self:solo:")
    # The turn ran.
    assert manager.calls and manager.calls[0][0] == "solo"
    # And it reported back like a member reply.
    assert recorder.replies and recorder.replies[0]["status"] == "done"
    assert recorder.replies[0]["excerpt"] == "done it"
    assert recorder.handled and recorder.handled[0].status == "done"


@pytest.mark.asyncio
async def test_stream_failure_reports_failed() -> None:
    recorder = _Recorder()

    class _Broken:
        async def stream(self, _agent_id: str, _request: dict[str, Any]):
            raise RuntimeError("model down")
            yield  # pragma: no cover

    autopilot = _autopilot(_Broken(), recorder)

    await autopilot.submit_peer("solo", "solo", "go", TARGET)
    await asyncio.sleep(0.05)  # let the background task run

    assert recorder.replies[0]["status"] == "failed"
    assert "model down" in recorder.replies[0]["excerpt"] or recorder.handled[0].status == "failed"


@pytest.mark.asyncio
async def test_hitl_pause_leaves_the_task_to_the_approval() -> None:
    """A pause must not close the task as ``done``."""
    recorder = _Recorder()
    manager = _AgentManager(
        [
            {"type": "token", "content": "thinking"},
            {"type": "hitl_required", "request": {"action_requests": []}},
        ]
    )
    autopilot = _autopilot(manager, recorder)

    await autopilot.submit_peer("solo", "solo", "go", TARGET)
    await asyncio.sleep(0.05)

    assert recorder.replies == []
    assert recorder.handled == []
