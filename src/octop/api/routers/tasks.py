"""Board tasks for experts and teams.

An agent's board is its own commitment ledger: a team host dispatches the rows
to its members, a digital employee executes them itself. Both share the same
table, state machine and CAS guard — see ``api.common.team_board``.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from octop.api.common.agent import assert_agent_access_row, assert_agent_owner
from octop.api.common.team_board import create_task, list_tasks, patch_task, task_payload
from octop.api.deps import current_user, get_server
from octop.infra.agents.teams import is_team_agent
from octop.infra.agents.teams.board_tools import task_updated_frame
from octop.infra.agents.teams.drive import TEAM_DRIVE_OPEN_TASK_LIMIT
from octop.infra.agents.teams.task_state import TASK_STATUSES
from octop.infra.db.repos.team_task_reviews import REVIEW_APPROVED
from octop.infra.errors import ErrorCode, OctopError

router = APIRouter()


class TaskCreateBody(BaseModel):
    title: str = Field(..., min_length=1, max_length=200, description="Task title, one line.")
    spec: str = Field("", max_length=8000, description="Self-contained assignment brief.")
    goal_id: str | None = Field(None, description="Goal this task serves, if any.")
    assignee: str | None = Field(
        None,
        description=(
            "Agent to execute the task. Must be a member of the team, or the "
            "agent itself for a digital employee."
        ),
    )
    priority: int = Field(0, ge=-1000, le=1000, description="Higher runs first.")
    due_at: int | None = Field(None, description="Unix timestamp deadline.")


class TaskPatchBody(BaseModel):
    status: str | None = Field(
        None,
        description="New status. Legal: " + ", ".join(sorted(TASK_STATUSES)) + ".",
    )
    priority: int | None = Field(None, description="Higher runs first.")
    due_at: int | None = Field(None, description="Unix timestamp deadline.")
    expected_version: int = Field(..., description="CAS guard from the last read.")


class TaskReviewBody(BaseModel):
    decision: Literal["approved", "rejected"] = Field(
        ..., description="Approve the delivery or send it back for rework."
    )
    comment: str = Field("", max_length=2000, description="What was checked.")
    expected_version: int = Field(..., description="CAS guard from the last read.")


def _repos(server: Any) -> Any:
    assert server.services is not None
    return server.services.repos


def _owned_agent(server: Any, user: Any, agent_id: str) -> Any:
    """Load the agent and require access (owner, shared, or admin)."""
    # Read the row from the repo, not the registry: the registry only knows
    # agents the runtime has loaded, so an idle agent would 404 here.
    row = _repos(server).agent_repo.get(agent_id)
    if row is None:
        raise OctopError(ErrorCode.AGENT_NOT_FOUND, f"agent {agent_id!r} not found")
    assert_agent_access_row(row, user)
    return row


def _assignee_error(server: Any, row: Any, assignee: str) -> str | None:
    """Reject an assignee that could not execute on this board."""
    agent_id = row.agent_id
    team_host = is_team_agent(row)
    autonomous = bool(getattr(row, "autonomy_enabled", 0))
    if autonomous and not team_host and assignee != agent_id:
        return "autonomous agents may only assign tasks to themselves"
    if team_host:
        roster = set(server.app_runtime.agent_registry.teams.member_ids(agent_id))
        if assignee not in roster:
            return f"assignee is not a member of this team: {assignee}"
    return None


def _notify(server: Any, user: Any, row: Any) -> Any:
    """Board event + ledger entry; returns the coroutine for the caller to await."""
    runtime = server.app_runtime
    if runtime is None:
        return None
    return runtime.gateway.ws_hub.push_to_user(user.id, task_updated_frame(row))


def _record(server: Any, row: Any, user: Any) -> None:
    coordinator = getattr(getattr(server.app_runtime, "gateway", None), "team_coordinator", None)
    if coordinator is None:
        return
    # The route pushed the WS frame itself; ask the coordinator not to repeat it.
    coordinator.record_task_change(row, actor=f"user:{user.id}", emit=False)


@router.get("/agents/{agent_id}/tasks", summary="List an agent's tasks")
async def list_agent_tasks(
    agent_id: str,
    status: str | None = None,
    goal_id: str | None = None,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> list[dict[str, Any]]:
    """Board tasks for a team host or a digital employee."""
    _owned_agent(server, user, agent_id)
    return list_tasks(_repos(server).team_task_repo, agent_id, status=status, goal_id=goal_id)


@router.post("/agents/{agent_id}/tasks", status_code=201, summary="Create an agent task")
async def create_agent_task(
    agent_id: str,
    body: TaskCreateBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Add one task to the board.

    ``assignee`` must be able to execute it: a member of this team, or the
    agent itself for a digital employee. The open-task cap is the same one the
    host tools enforce.
    """
    row = _owned_agent(server, user, agent_id)
    assert_agent_owner(row, user)
    repos = _repos(server)
    if repos.team_task_repo.count_open(agent_id) >= TEAM_DRIVE_OPEN_TASK_LIMIT:
        raise OctopError(
            ErrorCode.TEAM_TASK_LIMIT_REACHED,
            f"open task limit reached ({TEAM_DRIVE_OPEN_TASK_LIMIT})",
            details={"limit": TEAM_DRIVE_OPEN_TASK_LIMIT},
        )
    if body.goal_id is not None:
        goal = repos.team_goal_repo.get(body.goal_id)
        if goal is None or goal.team_id != agent_id:
            raise OctopError(ErrorCode.NOT_FOUND, f"goal {body.goal_id!r} not found")
    assignee = body.assignee
    if assignee is None and bool(getattr(row, "autonomy_enabled", 0)) and not is_team_agent(row):
        # A digital employee executes its own rows; an unassigned task would
        # be escalated as "no assignee" and never run.
        assignee = agent_id
    if assignee is not None:
        problem = _assignee_error(server, row, assignee)
        if problem is not None:
            raise OctopError(ErrorCode.TEAM_TASK_ASSIGNEE_INVALID, problem)
        if repos.agent_repo.get(body.assignee) is None:
            raise OctopError(ErrorCode.AGENT_NOT_FOUND, f"assignee {body.assignee!r} not found")
    created = create_task(
        repos.team_task_repo,
        agent_id=agent_id,
        title=body.title,
        spec=body.spec,
        goal_id=body.goal_id,
        assignee=assignee,
        priority=body.priority,
        due_at=body.due_at,
    )
    push = _notify(server, user, created)
    if push is not None:
        await push
    _record(server, created, user)
    return task_payload(created)


@router.patch("/agents/{agent_id}/tasks/{task_id}", summary="Update an agent task (CAS)")
async def patch_agent_task(
    agent_id: str,
    task_id: str,
    body: TaskPatchBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """CAS update with server-side state-machine validation.

    ``400`` for an illegal transition, ``409`` for a stale ``expected_version``
    or a pending approval on the task, ``403`` when the caller does not own the
    agent.
    """
    row = _owned_agent(server, user, agent_id)
    assert_agent_owner(row, user)
    repos = _repos(server)
    updated, _terminal = patch_task(
        repos.team_task_repo,
        repos.team_wait_repo,
        agent_id=agent_id,
        task_id=task_id,
        status=body.status,
        priority=body.priority,
        due_at=body.due_at,
        expected_version=body.expected_version,
    )
    push = _notify(server, user, updated)
    if push is not None:
        await push
    _record(server, updated, user)
    return task_payload(updated)


@router.post(
    "/agents/{agent_id}/tasks/{task_id}/review",
    summary="Accept or reject a task in review",
)
async def review_agent_task(
    agent_id: str,
    task_id: str,
    body: TaskReviewBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Record an acceptance decision and move the task accordingly.

    Approval closes the task; rejection sends it back to ``proposed`` so the
    decision loop (or the employee itself) picks it up again. The decision is
    stored with its reviewer and comment, so an approved task says who
    accepted it and what they checked.
    """
    row = _owned_agent(server, user, agent_id)
    assert_agent_owner(row, user)
    repos = _repos(server)
    current = repos.team_task_repo.get(task_id)
    if current is None or current.team_id != agent_id:
        raise OctopError(ErrorCode.NOT_FOUND, f"task {task_id!r} not found")
    if current.status != "review":
        raise OctopError(
            ErrorCode.TEAM_TASK_INVALID_TRANSITION,
            f"task is not awaiting review (status {current.status})",
            details={"task_id": task_id, "status": current.status},
        )
    target = "done" if body.decision == REVIEW_APPROVED else "proposed"
    updated, _terminal = patch_task(
        repos.team_task_repo,
        repos.team_wait_repo,
        agent_id=agent_id,
        task_id=task_id,
        status=target,
        expected_version=body.expected_version,
    )
    review = repos.team_task_review_repo.create(
        task_id=task_id,
        decision=body.decision,
        comment=body.comment.strip(),
        reviewer=f"user:{user.id}",
    )
    push = _notify(server, user, updated)
    if push is not None:
        await push
    _record(server, updated, user)
    return {
        "task": task_payload(updated),
        "review": {
            "review_id": review.review_id,
            "decision": review.decision,
            "comment": review.comment,
            "reviewer": review.reviewer,
            "created_at": review.created_at,
        },
    }


@router.get(
    "/agents/{agent_id}/tasks/{task_id}/reviews",
    summary="List a task's acceptance records",
)
async def list_task_reviews(
    agent_id: str,
    task_id: str,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> list[dict[str, Any]]:
    row = _owned_agent(server, user, agent_id)
    assert_agent_owner(row, user)
    task = _repos(server).team_task_repo.get(task_id)
    if task is None or task.team_id != agent_id:
        raise OctopError(ErrorCode.NOT_FOUND, f"task {task_id!r} not found")
    return [
        {
            "review_id": item.review_id,
            "decision": item.decision,
            "comment": item.comment,
            "reviewer": item.reviewer,
            "created_at": item.created_at,
        }
        for item in _repos(server).team_task_review_repo.list_for_task(task_id)
    ]


__all__ = ["router", "TaskCreateBody", "TaskPatchBody", "TaskReviewBody"]
