"""Shared goal/task board logic for team and agent-generic HTTP routes.

``/teams/{id}/tasks`` and ``/agents/{id}/tasks`` must validate the same state
machine and the same CAS guard. Keeping one implementation here is what stops
the server from growing two answers to "is this a legal transition" — the
exact split that lets the server reject a write it just performed itself.
"""

from __future__ import annotations

from typing import Any

from octop.infra.agents.teams.task_state import is_valid_task_transition
from octop.infra.db.repos.team_tasks import TeamTaskRepo
from octop.infra.db.repos.team_waits import WAIT_KIND_APPROVAL, WAIT_STATUS_CANCELLED, TeamWaitRepo
from octop.infra.errors import ErrorCode, OctopError

TERMINAL_TASK_STATUSES = frozenset({"done", "failed", "cancelled"})


def goal_payload(row: Any, *, deliverables: list[Any] | None = None) -> dict[str, Any]:
    """Serialize a goal row. One shape for every route that returns one."""
    payload: dict[str, Any] = {
        "goal_id": row.goal_id,
        "agent_id": row.team_id,
        "team_id": row.team_id,
        "title": row.title,
        "description": row.description,
        "status": row.status,
        "success_criteria": row.success_criteria,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
    if deliverables is not None:
        payload["deliverables"] = deliverables
    return payload


def task_payload(row: Any) -> dict[str, Any]:
    return {
        "task_id": row.task_id,
        "team_id": row.team_id,
        "goal_id": row.goal_id,
        "title": row.title,
        "spec": row.spec,
        "assignee": row.assignee,
        "status": row.status,
        "priority": row.priority,
        "result_summary": row.result_summary,
        "blocked_reason": row.blocked_reason,
        "version": row.version,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "due_at": row.due_at,
        "window_start": row.window_start,
        "window_end": row.window_end,
        "budget_tokens": row.budget_tokens,
        "concurrency_key": row.concurrency_key,
        "active_wait_id": row.active_wait_id,
    }


def list_tasks(
    repo: TeamTaskRepo,
    agent_id: str,
    *,
    status: str | None = None,
    goal_id: str | None = None,
) -> list[dict[str, Any]]:
    return [
        task_payload(row) for row in repo.list_by_team(agent_id, status=status, goal_id=goal_id)
    ]


def patch_task(
    repo: TeamTaskRepo,
    wait_repo: TeamWaitRepo,
    *,
    agent_id: str,
    task_id: str,
    status: str | None = None,
    priority: int | None = None,
    due_at: int | None = None,
    expected_version: int,
) -> tuple[Any, bool]:
    """Validate and CAS one board task.

    Returns ``(updated_row, terminal)``. Raises ``OctopError`` for an unknown
    task, an illegal transition, a pending approval conflict, or a version
    mismatch. Side effects (WebSocket frame, event ledger) belong to the
    caller so team and agent routes can differ there.
    """
    current = repo.get(task_id)
    if current is None or current.team_id != agent_id:
        raise OctopError(ErrorCode.NOT_FOUND, f"task {task_id!r} not found")
    if status is not None and not is_valid_task_transition(current.status, status):
        raise OctopError(
            ErrorCode.TEAM_TASK_INVALID_TRANSITION,
            f"illegal transition {current.status} -> {status}",
            details={"from_status": current.status, "to_status": status},
        )
    if (
        status is not None
        and status not in ("needs_input", "done", "failed", "cancelled")
        and current.status == "needs_input"
        and wait_repo.list_open_for_task(task_id, kind=WAIT_KIND_APPROVAL)
    ):
        raise OctopError(
            ErrorCode.TEAM_WAIT_CONFLICT,
            "resolve the pending approval before changing this task",
            details={"task_id": task_id},
        )

    terminal = status in TERMINAL_TASK_STATUSES
    fields: dict[str, object] = {}
    if status is not None:
        fields["status"] = status
    if priority is not None:
        fields["priority"] = priority
    if due_at is not None:
        fields["due_at"] = due_at
    if terminal:
        fields["active_wait_id"] = None
    updated = repo.update(task_id, expected_version=expected_version, fields=fields)
    if updated is None:
        raise OctopError(
            ErrorCode.TEAM_TASK_CONFLICT,
            "task was modified concurrently; reload and retry",
            details={"task_id": task_id},
        )
    if terminal:
        for wait in wait_repo.list_open_for_task(task_id):
            wait_repo.settle(wait.wait_id, status=WAIT_STATUS_CANCELLED)
    return updated, terminal


def create_task(
    repo: TeamTaskRepo,
    *,
    agent_id: str,
    title: str,
    spec: str = "",
    goal_id: str | None = None,
    assignee: str | None = None,
    priority: int = 0,
    due_at: int | None = None,
) -> Any:
    """Create one board task for ``agent_id``.

    Assignee and goal ownership are the caller's responsibility: a team checks
    its roster, a digital employee may only assign itself.
    """
    return repo.create(
        team_id=agent_id,
        title=title.strip(),
        goal_id=goal_id,
        spec=spec.strip(),
        assignee=assignee,
        priority=int(priority),
        due_at=due_at,
    )
