"""Goals and deliverables for experts and teams.

The existing ``team_goals`` table is keyed by the owning agent id, so the same
rows can back an expert goal or a team goal. Team goals additionally update the
team runtime / event ledger; expert goals are user-facing tracking only.
"""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from octop.api.common.agent import assert_agent_owner
from octop.api.deps import current_user, get_server
from octop.infra.agents.teams import TeamCoordinator, is_team_agent
from octop.infra.agents.threads.artifact import artifact_path_allowed
from octop.infra.errors import ErrorCode, OctopError

router = APIRouter()

_MAX_DELIVERABLES = 20
_MAX_TEXT_ARTIFACT = 4000


class DeliverableCreateBody(BaseModel):
    title: str = Field(..., min_length=1, max_length=200, description="Expected output name.")
    description: str = Field("", max_length=1000, description="What the output should contain.")
    format: str = Field(
        "", max_length=200, description="Format or delivery channel (doc, link, …)."
    )
    required: bool = Field(True, description="Whether the goal is incomplete without it.")


class GoalCreateBody(BaseModel):
    title: str = Field(..., min_length=1, max_length=200, description="Goal title, one line.")
    description: str = Field("", max_length=5000, description="Background and constraints.")
    success_criteria: str = Field("", max_length=5000, description="What done looks like.")
    deliverables: list[DeliverableCreateBody] = Field(
        default_factory=list,
        max_length=_MAX_DELIVERABLES,
        description="Expected outputs preset by the creator.",
    )


class GoalPatchBody(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=5000)
    success_criteria: str | None = Field(None, max_length=5000)


class DeliverablePatchBody(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=1000)
    format: str | None = Field(None, max_length=200)
    required: bool | None = None
    status: Literal["pending", "produced"] | None = None
    task_id: str | None = Field(None, max_length=80)
    artifact_type: Literal["none", "file", "link", "text"] | None = None
    artifact_ref: str | None = Field(None, max_length=_MAX_TEXT_ARTIFACT)
    artifact_name: str | None = Field(None, max_length=200)
    artifact_agent_id: str | None = Field(None, max_length=80)


def _repos(server: Any) -> Any:
    assert server.services is not None
    return server.services.repos


def _owned_agent(server: Any, user: Any, agent_id: str) -> Any:
    row = _repos(server).agent_repo.get(agent_id)
    if row is None:
        raise OctopError(ErrorCode.AGENT_NOT_FOUND, f"agent {agent_id!r} not found")
    assert_agent_owner(row, user)
    return row


def _owned_goal(server: Any, user: Any, agent_id: str, goal_id: str) -> tuple[Any, Any]:
    row = _owned_agent(server, user, agent_id)
    goal = _repos(server).team_goal_repo.get(goal_id)
    if goal is None or goal.team_id != agent_id:
        raise OctopError(ErrorCode.NOT_FOUND, f"goal {goal_id!r} not found")
    return row, goal


def _deliverable_payload(row: Any) -> dict[str, Any]:
    return {
        "deliverable_id": row.deliverable_id,
        "goal_id": row.goal_id,
        "title": row.title,
        "description": row.description,
        "format": row.format,
        "required": row.required,
        "status": row.status,
        "task_id": row.task_id,
        "artifact_type": row.artifact_type,
        "artifact_ref": row.artifact_ref,
        "artifact_name": row.artifact_name,
        "artifact_agent_id": row.artifact_agent_id,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _goal_payload(server: Any, row: Any) -> dict[str, Any]:
    deliverables = _repos(server).goal_deliverable_repo.list_for_goal(row.goal_id)
    return {
        "goal_id": row.goal_id,
        "agent_id": row.team_id,
        "team_id": row.team_id,
        "title": row.title,
        "description": row.description,
        "status": row.status,
        "success_criteria": row.success_criteria,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "deliverables": [_deliverable_payload(item) for item in deliverables],
    }


def _record_team_goal(server: Any, agent_id: str, row: Any, *, user: Any) -> None:
    coordinator = getattr(getattr(server.app_runtime, "gateway", None), "team_coordinator", None)
    if coordinator is None:
        assert server.services is not None
        coordinator = TeamCoordinator(
            event_repo=server.services.repos.team_event_repo,
            runtime_repo=server.services.repos.team_runtime_repo,
        )
    coordinator.record_goal_change(row, actor=f"user:{user.id}")
    runtime = _repos(server).team_runtime_repo.ensure(agent_id)
    _repos(server).team_runtime_repo.update(
        agent_id, expected_version=runtime.version, fields={"goal_id": row.goal_id}
    )


def _push_goal_frame(server: Any, agent_id: str) -> None:
    runtime = getattr(server, "app_runtime", None)
    if runtime is None or runtime.gateway is None:
        return
    with_warning = getattr(runtime.gateway, "push_team_frame", None)
    if callable(with_warning):
        with_warning(agent_id, {"type": "team.goal.updated", "team_id": agent_id})


def _artifact_agent_allowed(server: Any, agent_id: str, owner_row: Any, aid: str) -> bool:
    if not aid:
        return False
    target = _repos(server).agent_repo.get(aid)
    if target is None:
        return False
    if aid == agent_id:
        return True
    if is_team_agent(owner_row):
        try:
            return aid in set(server.app_runtime.agent_registry.teams.member_ids(agent_id))
        except Exception:
            return False
    return False


def _validate_artifact_fields(
    server: Any,
    agent_id: str,
    owner_row: Any,
    fields: dict[str, Any],
    current: Any,
) -> None:
    artifact_type = fields.get("artifact_type", current.artifact_type)
    ref = str(fields.get("artifact_ref", current.artifact_ref) or "").strip()
    aid = str(fields.get("artifact_agent_id", current.artifact_agent_id) or agent_id)
    status = fields.get("status", current.status)
    if artifact_type == "none":
        if status == "produced":
            raise OctopError(
                ErrorCode.GOAL_DELIVERABLE_INVALID,
                "produced deliverables need a file, link or text artifact",
            )
        fields["artifact_type"] = "none"
        fields["artifact_ref"] = ""
        fields["artifact_name"] = ""
        fields["artifact_agent_id"] = None
        return
    if not ref:
        raise OctopError(ErrorCode.GOAL_DELIVERABLE_INVALID, "artifact reference is required")
    if artifact_type == "file":
        if not artifact_path_allowed(ref):
            raise OctopError(ErrorCode.GOAL_DELIVERABLE_INVALID, "artifact path is not allowed")
        if not _artifact_agent_allowed(server, agent_id, owner_row, aid):
            raise OctopError(
                ErrorCode.GOAL_DELIVERABLE_INVALID,
                "artifact agent must own the goal or be a team member",
            )
        fields["artifact_agent_id"] = aid
    elif artifact_type == "link":
        parsed = urlparse(ref)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise OctopError(ErrorCode.GOAL_DELIVERABLE_INVALID, "artifact link must be http(s)")
        fields["artifact_agent_id"] = None
    elif artifact_type == "text":
        if len(ref) > _MAX_TEXT_ARTIFACT:
            raise OctopError(ErrorCode.GOAL_DELIVERABLE_INVALID, "artifact text is too long")
        fields["artifact_agent_id"] = None
    fields["artifact_ref"] = ref


@router.get("/agents/{agent_id}/goals", summary="List an expert or team's goals")
async def list_agent_goals(
    agent_id: str,
    status: str | None = None,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> list[dict[str, Any]]:
    _owned_agent(server, user, agent_id)
    rows = _repos(server).team_goal_repo.list_by_team(agent_id, status=status)
    return [_goal_payload(server, row) for row in rows]


@router.post("/agents/{agent_id}/goals", summary="Set an expert or team's active goal")
async def create_agent_goal(
    agent_id: str,
    body: GoalCreateBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    row = _owned_agent(server, user, agent_id)
    repos = _repos(server)
    repos.team_goal_repo.archive_active(agent_id)
    goal = repos.team_goal_repo.create(
        team_id=agent_id,
        title=body.title.strip(),
        created_by=user.id,
        description=body.description.strip(),
        success_criteria=body.success_criteria.strip(),
    )
    for item in body.deliverables:
        repos.goal_deliverable_repo.create(
            goal_id=goal.goal_id,
            title=item.title.strip(),
            created_by=user.id,
            description=item.description.strip(),
            format=item.format.strip(),
            required=item.required,
        )
    if is_team_agent(row):
        _record_team_goal(server, agent_id, goal, user=user)
        _push_goal_frame(server, agent_id)
    return _goal_payload(server, goal)


@router.patch("/agents/{agent_id}/goals/{goal_id}", summary="Update an expert or team goal")
async def patch_agent_goal(
    agent_id: str,
    goal_id: str,
    body: GoalPatchBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    row, goal = _owned_goal(server, user, agent_id, goal_id)
    fields: dict[str, Any] = {}
    if body.title is not None:
        fields["title"] = body.title.strip()
    if body.description is not None:
        fields["description"] = body.description.strip()
    if body.success_criteria is not None:
        fields["success_criteria"] = body.success_criteria.strip()
    updated = _repos(server).team_goal_repo.update(goal.goal_id, fields=fields) if fields else goal
    if updated is None:
        raise OctopError(ErrorCode.NOT_FOUND, f"goal {goal_id!r} not found")
    if is_team_agent(row):
        _record_team_goal(server, agent_id, updated, user=user)
        _push_goal_frame(server, agent_id)
    return _goal_payload(server, updated)


@router.post(
    "/agents/{agent_id}/goals/{goal_id}/deliverables",
    summary="Add an expected deliverable to a goal",
)
async def create_deliverable(
    agent_id: str,
    goal_id: str,
    body: DeliverableCreateBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    _owner_row, goal = _owned_goal(server, user, agent_id, goal_id)
    row = _repos(server).goal_deliverable_repo.create(
        goal_id=goal.goal_id,
        title=body.title.strip(),
        created_by=user.id,
        description=body.description.strip(),
        format=body.format.strip(),
        required=body.required,
    )
    _push_goal_frame(server, agent_id)
    return _deliverable_payload(row)


@router.patch(
    "/agents/{agent_id}/goals/{goal_id}/deliverables/{deliverable_id}",
    summary="Attach a file, link or text artifact to a deliverable",
)
async def patch_deliverable(
    agent_id: str,
    goal_id: str,
    deliverable_id: str,
    body: DeliverablePatchBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    owner_row, goal = _owned_goal(server, user, agent_id, goal_id)
    current = _repos(server).goal_deliverable_repo.get(deliverable_id)
    if current is None or current.goal_id != goal.goal_id:
        raise OctopError(ErrorCode.NOT_FOUND, f"deliverable {deliverable_id!r} not found")
    fields: dict[str, Any] = {}
    for key in ("title", "description", "format", "required", "status", "task_id"):
        value = getattr(body, key)
        if value is not None:
            fields[key] = value.strip() if isinstance(value, str) else value
    for key in ("artifact_type", "artifact_ref", "artifact_name", "artifact_agent_id"):
        value = getattr(body, key)
        if value is not None:
            fields[key] = value
    if "task_id" in fields and fields["task_id"]:
        task = _repos(server).team_task_repo.get(str(fields["task_id"]))
        if task is None or task.team_id != agent_id:
            raise OctopError(
                ErrorCode.GOAL_DELIVERABLE_INVALID, "task does not belong to this goal"
            )
    if any(key.startswith("artifact_") for key in fields) or "status" in fields:
        _validate_artifact_fields(server, agent_id, owner_row, fields, current)
    updated = _repos(server).goal_deliverable_repo.update(deliverable_id, fields=fields)
    if updated is None:
        raise OctopError(ErrorCode.NOT_FOUND, f"deliverable {deliverable_id!r} not found")
    _push_goal_frame(server, agent_id)
    return _deliverable_payload(updated)


@router.delete(
    "/agents/{agent_id}/goals/{goal_id}/deliverables/{deliverable_id}",
    status_code=204,
    summary="Remove an expected deliverable",
)
async def delete_deliverable(
    agent_id: str,
    goal_id: str,
    deliverable_id: str,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> None:
    _owner_row, goal = _owned_goal(server, user, agent_id, goal_id)
    current = _repos(server).goal_deliverable_repo.get(deliverable_id)
    if current is None or current.goal_id != goal.goal_id:
        raise OctopError(ErrorCode.NOT_FOUND, f"deliverable {deliverable_id!r} not found")
    _repos(server).goal_deliverable_repo.delete(deliverable_id)
    _push_goal_frame(server, agent_id)
