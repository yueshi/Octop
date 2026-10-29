"""Digital-employee switch and job spec.

Two resources, deliberately separate:

* ``/agents/{id}/autonomy`` — the on/off switch (``agents.autonomy_enabled``).
  Turning it off must clean up like a team autopilot stop, not merely flip a
  boolean, or the agent keeps running while nobody considers it employed.
* ``/agents/{id}/autonomy-policy`` — the job parameters (duty window, budget,
  SLA, escalation target, scope). Stored on ``proactive_care_config``; see
  ``repos.autonomy_policy``.
"""

from __future__ import annotations

import time as _time
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator

from octop.api.common.agent import assert_agent_owner
from octop.api.deps import current_user, get_server
from octop.infra.agents.teams import is_team_agent
from octop.infra.agents.teams.dispatcher import budget_remaining
from octop.infra.agents.teams.task_state import OPEN_TASK_STATUSES
from octop.infra.db.repos.autonomy_policy import AutonomyPolicy
from octop.infra.db.repos.team_waits import WAIT_KIND_APPROVAL
from octop.infra.errors import ErrorCode, OctopError

router = APIRouter()


class AutonomySwitchBody(BaseModel):
    enabled: bool = Field(..., description="True hires the agent as a digital employee.")


class AutonomyState(BaseModel):
    agent_id: str
    enabled: bool


class AutonomyPolicyBody(BaseModel):
    active_hours_start: str = Field("09:00", pattern=r"^\d{2}:\d{2}$")
    active_hours_end: str = Field("22:00", pattern=r"^\d{2}:\d{2}$")
    min_interval_hours: int = Field(5, ge=1, le=168)
    max_interval_hours: int = Field(24, ge=1, le=720)
    budget_limit_tokens: int = Field(0, ge=0, description="0 means unlimited.")
    budget_window: Literal["day", "month"] = Field("day")
    sla_tasks_per_day: int = Field(0, ge=0, description="0 means unlimited.")
    escalate_to: str = Field("", max_length=200, description="Who receives escalations.")
    responsible_scope: str = Field("", max_length=4000, description="Duties and boundaries.")
    tool_scope_allow: str = Field("", max_length=4000, description="Comma-separated tools.")
    tool_scope_deny: str = Field("", max_length=4000, description="Comma-separated tools.")

    @model_validator(mode="after")
    def _check_window(self) -> AutonomyPolicyBody:
        if self.active_hours_start >= self.active_hours_end:
            raise ValueError("active_hours_start must be before active_hours_end")
        if self.min_interval_hours > self.max_interval_hours:
            raise ValueError("min_interval_hours must not exceed max_interval_hours")
        return self


class AutonomyPolicyPayload(AutonomyPolicyBody):
    agent_id: str


def _repos(server: Any) -> Any:
    assert server.services is not None
    return server.services.repos


def _owned_agent(server: Any, user: Any, agent_id: str) -> Any:
    # Read the row from the repo, not the registry: the registry only knows
    # agents the runtime has loaded, so an idle agent would 404 here.
    row = _repos(server).agent_repo.get(agent_id)
    if row is None:
        raise OctopError(ErrorCode.AGENT_NOT_FOUND, f"agent {agent_id!r} not found")
    assert_agent_owner(row, user)
    return row


async def _stop_drive(server: Any, agent_id: str) -> None:
    """Clean up a running loop before the switch goes off."""
    runtime = server.app_runtime
    autopilot = getattr(getattr(runtime, "gateway", None), "team_autopilot", None)
    if autopilot is None:
        return
    await autopilot.stop(
        agent_id,
        cron_manager=getattr(runtime, "cron_manager", None),
        team_inbox=runtime.agent_registry.team_inbox(),
    )


@router.put("/agents/{agent_id}/autonomy", summary="Enable or disable a digital employee")
async def set_autonomy(
    agent_id: str,
    body: AutonomySwitchBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> AutonomyState:
    """Hire or dismiss the agent.

    Disabling runs the same cleanup as stopping a team autopilot: in-flight
    work is cancelled and open waits are settled, so the agent is not left
    producing results nobody is tracking. Team hosts keep their own autopilot
    switch — setting this on one is rejected rather than silently ambiguous.
    """
    row = _owned_agent(server, user, agent_id)
    if is_team_agent(row):
        raise OctopError(
            ErrorCode.TEAM_NOT_SHAREABLE,
            "team hosts use their own autopilot switch, not agent autonomy",
        )
    was_enabled = bool(getattr(row, "autonomy_enabled", 0))
    if not body.enabled and was_enabled:
        await _stop_drive(server, agent_id)
    _repos(server).agent_repo.set_autonomy_enabled(agent_id, body.enabled)
    assert server.app_runtime is not None
    await server.app_runtime.agent_registry.reload(agent_id)
    if body.enabled:
        # Hiring means the loop actually runs: without this the agent has
        # board tools but never wakes on its own.
        autopilot = getattr(getattr(server.app_runtime, "gateway", None), "team_autopilot", None)
        if autopilot is not None:
            await autopilot.start(agent_id, user_id=user.id)
    return AutonomyState(agent_id=agent_id, enabled=body.enabled)


def _policy_payload(policy: AutonomyPolicy) -> dict[str, Any]:
    return {
        "agent_id": policy.agent_id,
        "active_hours_start": policy.active_hours_start,
        "active_hours_end": policy.active_hours_end,
        "min_interval_hours": policy.min_interval_hours,
        "max_interval_hours": policy.max_interval_hours,
        "budget_limit_tokens": policy.budget_limit_tokens,
        "budget_window": policy.budget_window,
        "sla_tasks_per_day": policy.sla_tasks_per_day,
        "escalate_to": policy.escalate_to,
        "responsible_scope": policy.responsible_scope,
        "tool_scope_allow": policy.tool_scope_allow,
        "tool_scope_deny": policy.tool_scope_deny,
    }


@router.get(
    "/agents/{agent_id}/autonomy-policy",
    summary="Read an agent's job spec",
    response_model=AutonomyPolicyPayload,
)
async def get_autonomy_policy(
    agent_id: str,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    _owned_agent(server, user, agent_id)
    return _policy_payload(_repos(server).autonomy_policy_repo.get(agent_id))


@router.put(
    "/agents/{agent_id}/autonomy-policy",
    summary="Update an agent's job spec",
    response_model=AutonomyPolicyPayload,
)
async def put_autonomy_policy(
    agent_id: str,
    body: AutonomyPolicyBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Save the job parameters. The on/off switch is a separate resource."""
    _owned_agent(server, user, agent_id)
    policy = AutonomyPolicy(agent_id=agent_id, **body.model_dump())
    _repos(server).autonomy_policy_repo.upsert(policy)
    return _policy_payload(policy)


__all__ = [
    "router",
    "AutonomyPolicyBody",
    "AutonomyState",
    "AutonomySwitchBody",
]


class InboxItem(BaseModel):
    kind: str = Field(description="approval / failed / overdue / budget")
    task_id: str | None = None
    title: str
    since: int | None = Field(None, description="When the item started waiting.")
    detail: str = ""


class InboxPayload(BaseModel):
    agent_id: str
    counts: dict[str, int]
    items: list[InboxItem]


class MetricsPayload(BaseModel):
    agent_id: str
    tasks: dict[str, int] = Field(description="Task counts by status, plus ``open``.")
    usage: dict[str, int] = Field(description="Token totals for the window.")
    budget: dict[str, int | None] = Field(
        description="``used`` / ``limit`` / ``remaining``; ``limit`` 0 means unlimited."
    )
    window: str


@router.get(
    "/agents/{agent_id}/inbox",
    summary="What needs a human on this agent's board",
    response_model=InboxPayload,
)
async def get_agent_inbox(
    agent_id: str,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Approvals waiting, failed work, overdue tasks, and a spent budget.

    One place to answer "does this employee need me right now" without
    reading the whole board.
    """
    _owned_agent(server, user, agent_id)
    repos = _repos(server)
    now = int(_time.time())
    items: list[InboxItem] = []

    for wait in repos.team_wait_repo.list_open_for_team(agent_id):
        if wait.kind != WAIT_KIND_APPROVAL:
            continue
        task = repos.team_task_repo.get(wait.task_id) if wait.task_id else None
        items.append(
            InboxItem(
                kind="approval",
                task_id=wait.task_id,
                title=(task.title if task is not None else "Approval"),
                since=wait.created_at,
                detail=str((wait.payload or {}).get("reason") or ""),
            )
        )

    for task in repos.team_task_repo.list_by_team(agent_id):
        if task.status == "failed":
            items.append(
                InboxItem(
                    kind="failed",
                    task_id=task.task_id,
                    title=task.title,
                    since=task.updated_at,
                    detail=task.blocked_reason or task.result_summary,
                )
            )
        elif task.status in OPEN_TASK_STATUSES and task.due_at and task.due_at < now:
            items.append(
                InboxItem(
                    kind="overdue",
                    task_id=task.task_id,
                    title=task.title,
                    since=task.due_at,
                    detail=f"due {task.due_at}",
                )
            )

    remaining = budget_remaining(repos, agent_id)
    if remaining == 0:
        items.append(
            InboxItem(
                kind="budget",
                since=None,
                title="Budget exhausted",
                detail="New work is deferred until the period rolls over or the limit rises.",
            )
        )

    counts: dict[str, int] = {}
    for item in items:
        counts[item.kind] = counts.get(item.kind, 0) + 1
    return {
        "agent_id": agent_id,
        "counts": counts,
        "items": [item.model_dump() for item in items],
    }


@router.get(
    "/agents/{agent_id}/metrics",
    summary="Delivery and spend for an agent",
    response_model=MetricsPayload,
)
async def get_agent_metrics(
    agent_id: str,
    window: str = "last_30d",
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Task counts by status plus token spend for ``window``."""
    _owned_agent(server, user, agent_id)
    repos = _repos(server)
    tasks: dict[str, int] = {}
    for task in repos.team_task_repo.list_by_team(agent_id):
        tasks[task.status] = tasks.get(task.status, 0) + 1
    tasks["open"] = repos.team_task_repo.count_open(agent_id)

    usage: dict[str, int] = {}
    if server.services is not None:
        # ``summary`` returns the totals flattened at the top level, not under
        # a ``totals`` key.
        summary = server.services.usage_repo.summary(agent_id=agent_id, window=window)
        usage = {
            "input_tokens": int(summary.get("input_tokens", 0) or 0),
            "output_tokens": int(summary.get("output_tokens", 0) or 0),
            "total_tokens": int(summary.get("total_tokens", 0) or 0),
            "model_calls": int(summary.get("model_calls", 0) or 0),
        }

    policy = repos.autonomy_policy_repo.get(agent_id)
    runtime = repos.team_runtime_repo.get(agent_id)
    return {
        "agent_id": agent_id,
        "tasks": tasks,
        "usage": usage,
        "budget": {
            "used": int(runtime.budget_used) if runtime is not None else 0,
            "limit": int(policy.budget_limit_tokens),
            "remaining": budget_remaining(repos, agent_id),
        },
        "window": window,
    }
