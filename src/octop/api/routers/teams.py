"""Expert teams — create, list, update roster."""

from __future__ import annotations

from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from octop.api.common.agent import assert_agent_owner
from octop.api.common.team_board import goal_payload as board_goal_payload
from octop.api.common.team_board import patch_task as board_patch_task
from octop.api.common.team_board import task_payload as board_task_payload
from octop.api.deps import current_user, get_server
from octop.infra.agents.manager import AgentCreateSpec
from octop.infra.agents.teams import (
    TEAM_KIND,
    TEAM_TEMPLATE_NAME,
    TEMPLATE_DIR,
    TeamCoordinator,
    is_team_agent,
    stop_team_drive,
    task_state,
    task_updated_frame,
    team_icon_url,
)
from octop.infra.agents.teams.runtime import TeamAutopilot
from octop.infra.db.repos.team_waits import (
    WAIT_KIND_APPROVAL,
    WAIT_STATUS_WAITING,
)
from octop.infra.errors import ErrorCode, OctopError

router = APIRouter()


class TeamCreateBody(BaseModel):
    name: str
    description: str | None = None
    default_model: str | None = None
    color: str | None = None
    icon_name: str | None = None
    icon_url: str | None = None
    welcome_message: str | None = None
    member_ids: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)


class TeamPatchBody(BaseModel):
    name: str | None = None
    description: str | None = None
    default_model: str | None = None
    color: str | None = None
    icon_name: str | None = None
    welcome_message: str | None = None
    member_ids: list[str] | None = None


class TeamGoalCreateBody(BaseModel):
    title: str = Field(description="Goal title, one line.")
    description: str = Field("", description="Background and constraints.")
    success_criteria: str = Field("", description="What done looks like.")


class TeamTaskPatchBody(BaseModel):
    status: str | None = Field(
        None,
        description="New status. Legal: " + ", ".join(sorted(task_state.TASK_STATUSES)) + ".",
    )
    priority: int | None = Field(None, description="Higher runs first.")
    due_at: int | None = Field(None, description="Unix timestamp deadline.")
    expected_version: int = Field(..., description="CAS guard from the last read.")


class TeamDriveStartBody(BaseModel):
    heartbeat_interval_sec: int | None = Field(
        None,
        description="Wake interval for heartbeat reconciliation (min 900). Default 1800.",
    )
    trigger: str | None = Field(
        None,
        description="Optional legacy-style schedule: interval:<seconds> or date:<ISO8601>.",
    )


class TeamDrivePauseBody(BaseModel):
    reason: str | None = Field(None, description="Why the user paused the loop.")


class TeamRuntimeWaitPayload(BaseModel):
    wait_id: str = Field(description="Wait ledger id.")
    kind: str = Field(description="member_reply / approval / timer / budget.")
    status: str = Field(description="waiting / settled / timed_out / deferred / cancelled.")
    task_id: str | None = Field(None, description="Board task this wait parks, if any.")
    due_at: int | None = Field(None, description="Unix timestamp for timer waits.")
    timeout_at: int | None = Field(
        None, description="Unix timestamp after which to retry/escalate."
    )
    external_ref: str | None = Field(
        None,
        description="Member HITL pause id; when set the card must be resolved in its conversation.",
    )
    reason: str = Field("", description="Why the team is waiting, when the plan provided one.")


class TeamRuntimePayload(BaseModel):
    team_id: str
    status: str = Field(
        description="idle / running / deciding / dispatching / waiting / paused_* / finishing / stopped."
    )
    mode: str = Field(description="autopilot or manual.")
    goal_id: str | None = None
    active_decision_id: str | None = None
    last_decision_at: int | None = None
    next_heartbeat_at: int | None = None
    heartbeat_interval_sec: int
    budget_day: str = ""
    budget_used: int = 0
    budget_reserved: int = 0
    paused_reason: str | None = None
    version: int
    open_waits: list[TeamRuntimeWaitPayload] = Field(default_factory=list)


class TeamDecisionPayload(BaseModel):
    decision_id: str
    team_id: str
    status: str = Field(description="pending / running / committed / failed / abandoned.")
    mode: str
    reason: str
    snapshot_hash: str
    cost_tokens: int
    budget_reserved: int
    error: str | None = None
    plan: dict[str, Any] | None = Field(None, description="Committed DecisionPlan + apply result.")
    created_at: int
    started_at: int | None = None
    finished_at: int | None = None


class TeamWaitResolveBody(BaseModel):
    status: Literal["approved", "rejected"] = Field(
        description="Approve the parked action or reject it; a rejection routes a new decision."
    )
    note: str = Field("", max_length=500, description="Optional note recorded with the rejection.")


class TeamWaitPayload(BaseModel):
    wait_id: str
    team_id: str
    task_id: str | None = None
    kind: str
    status: str
    due_at: int | None = None
    timeout_at: int | None = None
    deadline_at: int | None = None
    external_ref: str | None = Field(None, description="Inbox job id / HITL pending id.")
    reason: str = Field("", description="Why the team is waiting, when the plan provided one.")
    attempts: int = 0
    created_at: int
    settled_at: int | None = None


class TeamWaitResolveResult(BaseModel):
    wait: TeamWaitPayload
    task: dict[str, Any] | None = Field(
        None, description="Task updated by the resolution, when the wait was task-scoped."
    )


def _teams(server: Any) -> Any:
    assert server.app_runtime is not None
    return server.app_runtime.agent_registry.teams


def _require_owned_team(server: Any, user: Any, team_id: str) -> Any:
    assert server.app_runtime is not None
    row = server.app_runtime.agent_registry.get_row(team_id)
    if row is None or not is_team_agent(row):
        raise OctopError(ErrorCode.TEAM_NOT_FOUND, f"team {team_id!r} not found")
    assert_agent_owner(row, user)
    return row


@router.get("/teams", summary="List my expert teams")
async def list_teams(
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], _teams(server).list_for_user(user.id))


@router.post("/teams", summary="Create an expert team")
async def create_team(
    body: TeamCreateBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    teams = _teams(server)
    member_ids = teams.validate_member_ids(user, body.member_ids)
    assert server.app_runtime is not None
    spec = AgentCreateSpec(
        name=body.name.strip(),
        user_id=user.id,
        description=body.description,
        default_model=body.default_model,
        color=body.color,
        icon_name=body.icon_name or "users",
        icon_url=team_icon_url(body.icon_url),
        welcome_message=body.welcome_message,
        template_name=TEAM_TEMPLATE_NAME,
        kind=TEAM_KIND,
        config=body.config,
        member_ids=member_ids,
        mcp_servers=[],
    )
    row = await server.app_runtime.agent_registry.create(spec)
    return cast(
        dict[str, Any],
        teams.team_payload(server.app_runtime.agent_registry.get_row(row.agent_id) or row),
    )


@router.get("/teams/template", summary="Preview default team workspace files")
async def team_template_files(
    _user: Any = Depends(current_user),
) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    if TEMPLATE_DIR.is_dir():
        for path in sorted(TEMPLATE_DIR.iterdir()):
            if path.is_file() and path.suffix.lower() == ".md" and not path.name.startswith("."):
                files.append(
                    {
                        "name": path.name,
                        "content": path.read_text(encoding="utf-8"),
                    }
                )
    return files


@router.get("/teams/{team_id}", summary="Get an expert team")
async def get_team(
    team_id: str,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    row = _require_owned_team(server, user, team_id)
    return cast(dict[str, Any], _teams(server).team_payload(row))


@router.patch("/teams/{team_id}", summary="Update an expert team")
async def patch_team(
    team_id: str,
    body: TeamPatchBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    _require_owned_team(server, user, team_id)
    teams = _teams(server)
    assert server.app_runtime is not None
    roster_changed = False
    if body.member_ids is not None:
        next_ids = teams.validate_member_ids(user, body.member_ids)
        teams.assert_roster_writable(team_id, next_ids)
        teams.replace_members(team_id, next_ids)
        roster_changed = True
    updates: dict[str, Any] = {}
    if body.name is not None:
        updates["name"] = body.name.strip()
    if body.description is not None:
        updates["description"] = body.description
    if body.default_model is not None:
        updates["default_model"] = body.default_model
    if body.color is not None:
        updates["color"] = body.color
    if body.icon_name is not None:
        updates["icon_name"] = body.icon_name
    if body.welcome_message is not None:
        updates["welcome_message"] = body.welcome_message
    if updates:
        await server.app_runtime.agent_registry.update(team_id, **updates)
    if roster_changed:
        await server.app_runtime.agent_registry.reload(team_id)
    row = server.app_runtime.agent_registry.get_row(team_id)
    if row is None:
        raise OctopError(ErrorCode.TEAM_NOT_FOUND, f"team {team_id!r} not found")
    return cast(dict[str, Any], teams.team_payload(row))


@router.delete("/teams/{team_id}", status_code=204, summary="Delete an expert team")
async def delete_team(
    team_id: str,
    force: bool = Query(
        default=False,
        description="Delete even when the team still owns light apps (their data is lost).",
    ),
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> None:
    _require_owned_team(server, user, team_id)
    assert server.app_runtime is not None
    await server.app_runtime.agent_registry.delete(team_id, force=force)


def _repos(server: Any) -> Any:
    assert server.services is not None
    return server.services.repos


def _coordinator(repos: Any) -> TeamCoordinator:
    """Shadow ledger writer for dashboard board writes (v2 §11.2 phase 1)."""
    return TeamCoordinator(
        event_repo=repos.team_event_repo,
        runtime_repo=repos.team_runtime_repo,
    )


def _autopilot(server: Any) -> TeamAutopilot:
    runtime = server.app_runtime
    autopilot = getattr(getattr(runtime, "gateway", None), "team_autopilot", None)
    if autopilot is None:
        raise OctopError(ErrorCode.INTERNAL_ERROR, "team autopilot is not available")
    return cast(TeamAutopilot, autopilot)


def _event_writer(server: Any, repos: Any) -> TeamCoordinator:
    """Prefer the gateway coordinator so user writes can route work."""
    runtime = server.app_runtime
    coordinator = getattr(getattr(runtime, "gateway", None), "team_coordinator", None)
    return cast(TeamCoordinator, coordinator) if coordinator is not None else _coordinator(repos)


def _team_coordinator(server: Any) -> TeamCoordinator:
    """The gateway coordinator owns wait settlement and routing."""
    runtime = server.app_runtime
    coordinator = getattr(getattr(runtime, "gateway", None), "team_coordinator", None)
    if coordinator is None:
        raise OctopError(ErrorCode.INTERNAL_ERROR, "team coordinator is not available")
    return cast(TeamCoordinator, coordinator)


def _decision_payload(row: Any) -> dict[str, Any]:
    return {
        "decision_id": row.decision_id,
        "team_id": row.team_id,
        "status": row.status,
        "mode": row.mode,
        "reason": row.reason,
        "snapshot_hash": row.snapshot_hash,
        "cost_tokens": row.cost_tokens,
        "budget_reserved": row.budget_reserved,
        "error": row.error,
        "plan": row.plan,
        "created_at": row.created_at,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
    }


def _wait_payload(row: Any) -> dict[str, Any]:
    return {
        "wait_id": row.wait_id,
        "team_id": row.team_id,
        "task_id": row.task_id,
        "kind": row.kind,
        "status": row.status,
        "due_at": row.due_at,
        "timeout_at": row.timeout_at,
        "deadline_at": row.deadline_at,
        "external_ref": row.external_ref,
        "reason": str((row.payload or {}).get("reason") or ""),
        "attempts": row.attempts,
        "created_at": row.created_at,
        "settled_at": row.settled_at,
    }


def _goal_payload(server: Any, row: Any) -> dict[str, Any]:
    """Same shape as ``/agents/{id}/goals`` so the board and the drawer agree."""
    deliverables = [
        {
            "deliverable_id": item.deliverable_id,
            "goal_id": item.goal_id,
            "title": item.title,
            "description": item.description,
            "format": item.format,
            "required": bool(item.required),
            "status": item.status,
            "task_id": item.task_id,
            "artifact_type": item.artifact_type,
            "artifact_ref": item.artifact_ref,
            "artifact_name": item.artifact_name,
            "artifact_agent_id": item.artifact_agent_id,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
        }
        for item in _repos(server).goal_deliverable_repo.list_for_goal(row.goal_id)
    ]
    return board_goal_payload(row, deliverables=deliverables)


def _task_payload(row: Any) -> dict[str, Any]:
    return board_task_payload(row)


@router.get("/teams/{team_id}/goals", summary="List a team's goals")
async def list_team_goals(
    team_id: str,
    status: str | None = None,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> list[dict[str, Any]]:
    _require_owned_team(server, user, team_id)
    repos = _repos(server)
    return [
        _goal_payload(server, row)
        for row in repos.team_goal_repo.list_by_team(team_id, status=status)
    ]


@router.post("/teams/{team_id}/goals", summary="Set a team's active goal")
async def create_team_goal(
    team_id: str,
    body: TeamGoalCreateBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    _require_owned_team(server, user, team_id)
    repos = _repos(server)
    # One active goal per team: setting a new one archives the previous.
    repos.team_goal_repo.archive_active(team_id)
    row = repos.team_goal_repo.create(
        team_id=team_id,
        title=body.title.strip(),
        created_by=user.id,
        description=body.description.strip(),
        success_criteria=body.success_criteria.strip(),
    )
    _event_writer(server, repos).record_goal_change(row, actor=f"user:{user.id}")
    runtime = repos.team_runtime_repo.ensure(team_id)
    repos.team_runtime_repo.update(
        team_id, expected_version=runtime.version, fields={"goal_id": row.goal_id}
    )
    return _goal_payload(server, row)


@router.get("/teams/{team_id}/tasks", summary="List a team's tasks")
async def list_team_tasks(
    team_id: str,
    status: str | None = None,
    goal_id: str | None = None,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> list[dict[str, Any]]:
    _require_owned_team(server, user, team_id)
    repos = _repos(server)
    return [
        _task_payload(row)
        for row in repos.team_task_repo.list_by_team(team_id, status=status, goal_id=goal_id)
    ]


@router.patch("/teams/{team_id}/tasks/{task_id}", summary="Update a team task (CAS)")
async def patch_team_task(
    team_id: str,
    task_id: str,
    body: TeamTaskPatchBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    _require_owned_team(server, user, team_id)
    repos = _repos(server)
    # Same validation and CAS as ``/agents/{id}/tasks``: one implementation,
    # so the server cannot disagree with itself about a legal transition.
    updated, _terminal = board_patch_task(
        repos.team_task_repo,
        repos.team_wait_repo,
        agent_id=team_id,
        task_id=task_id,
        status=body.status,
        priority=body.priority,
        due_at=body.due_at,
        expected_version=body.expected_version,
    )
    runtime = server.app_runtime
    if runtime is not None:
        await runtime.gateway.ws_hub.push_to_user(user.id, task_updated_frame(updated))
    # The route pushes the WS frame itself; ask the coordinator not to duplicate it.
    _event_writer(server, repos).record_task_change(updated, actor=f"user:{user.id}", emit=False)
    return _task_payload(updated)


class DriveStopResult(BaseModel):
    disabled_jobs: int = Field(description="Drive cron jobs removed.")
    drained_jobs: int = Field(description="Queued member dispatches cancelled.")
    cancelled_tasks: int = Field(description="Open tasks moved to cancelled.")
    cancelled_waits: int = Field(0, description="Open wait-ledger rows cancelled.")


@router.get(
    "/teams/{team_id}/runtime",
    summary="Get a team's autopilot runtime state",
    response_model=TeamRuntimePayload,
)
async def get_team_runtime(
    team_id: str,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Runtime status, active goal/decision, heartbeat and open waits."""
    _require_owned_team(server, user, team_id)
    return _autopilot(server).status_payload(team_id)


@router.post(
    "/teams/{team_id}/drive/start",
    summary="Start or resume a team's autopilot",
    response_model=TeamRuntimePayload,
)
async def start_team_drive_endpoint(
    team_id: str,
    body: TeamDriveStartBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Set ``team_runtime`` to running/autopilot and kick the first decision."""
    _require_owned_team(server, user, team_id)
    autopilot = _autopilot(server)
    await autopilot.start(
        team_id,
        user_id=user.id,
        heartbeat_interval_sec=body.heartbeat_interval_sec,
        trigger=body.trigger,
    )
    return autopilot.status_payload(team_id)


@router.post(
    "/teams/{team_id}/drive/pause",
    summary="Pause a team's autopilot",
    response_model=TeamRuntimePayload,
)
async def pause_team_drive_endpoint(
    team_id: str,
    body: TeamDrivePauseBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Pause new decisions/dispatches; in-flight waits are kept."""
    _require_owned_team(server, user, team_id)
    autopilot = _autopilot(server)
    await autopilot.pause(team_id, reason=body.reason)
    return autopilot.status_payload(team_id)


@router.post(
    "/teams/{team_id}/drive/resume",
    summary="Resume a paused team autopilot",
    response_model=TeamRuntimePayload,
)
async def resume_team_drive_endpoint(
    team_id: str,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Reactivate deferred waits and kick one decision."""
    _require_owned_team(server, user, team_id)
    autopilot = _autopilot(server)
    await autopilot.resume(team_id)
    return autopilot.status_payload(team_id)


@router.post(
    "/teams/{team_id}/drive/finish",
    summary="Mark the goal done and stop new work",
    response_model=TeamRuntimePayload,
)
async def finish_team_drive_endpoint(
    team_id: str,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Target completion: no new decisions/dispatches; in-flight work settles."""
    _require_owned_team(server, user, team_id)
    autopilot = _autopilot(server)
    await autopilot.finish(team_id)
    return autopilot.status_payload(team_id)


@router.get(
    "/teams/{team_id}/decisions",
    summary="List a team's decision audit",
    response_model=list[TeamDecisionPayload],
)
async def list_team_decisions(
    team_id: str,
    limit: int = 50,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> list[dict[str, Any]]:
    _require_owned_team(server, user, team_id)
    rows = _repos(server).team_decision_repo.list_by_team(team_id, limit=max(1, min(limit, 200)))
    return [_decision_payload(row) for row in rows]


@router.get(
    "/teams/{team_id}/waits",
    summary="List a team's wait ledger",
    response_model=list[TeamWaitPayload],
)
async def list_team_waits(
    team_id: str,
    status: str | None = None,
    limit: int = 100,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> list[dict[str, Any]]:
    _require_owned_team(server, user, team_id)
    rows = _repos(server).team_wait_repo.list_by_team(
        team_id, status=status, limit=max(1, min(limit, 500))
    )
    return [_wait_payload(row) for row in rows]


@router.post(
    "/teams/{team_id}/waits/{wait_id}/resolve",
    summary="Approve or reject a decision-level approval wait",
    response_model=TeamWaitResolveResult,
)
async def resolve_team_wait(
    team_id: str,
    wait_id: str,
    body: TeamWaitResolveBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Settle a plan-level approval and resume/block its task.

    Member HITL pauses carry an ``external_ref`` and must be answered through
    their conversation card so the paused member graph can resume; this route
    rejects those with ``409 TEAM_WAIT_CONFLICT``.
    """
    _require_owned_team(server, user, team_id)
    repos = _repos(server)
    wait = repos.team_wait_repo.get(wait_id)
    if wait is None or wait.team_id != team_id:
        raise OctopError(ErrorCode.NOT_FOUND, f"wait {wait_id!r} not found")
    if wait.kind != WAIT_KIND_APPROVAL:
        raise OctopError(ErrorCode.TEAM_WAIT_CONFLICT, "only approval waits can be resolved")
    if wait.status != WAIT_STATUS_WAITING:
        raise OctopError(ErrorCode.TEAM_WAIT_CONFLICT, "approval wait is no longer open")
    if wait.external_ref:
        raise OctopError(
            ErrorCode.TEAM_WAIT_CONFLICT,
            "member HITL pauses are resolved in the conversation",
        )
    coordinator = _team_coordinator(server)
    try:
        fresh, updated = coordinator.resolve_approval_wait(
            team_id,
            wait_id,
            status=body.status,
            actor=f"user:{user.id}",
            note=body.note,
        )
    except ValueError as exc:
        raise OctopError(ErrorCode.TEAM_WAIT_CONFLICT, str(exc)) from exc
    return {
        "wait": _wait_payload(fresh),
        "task": _task_payload(updated) if updated is not None else None,
    }


@router.post("/teams/{team_id}/drive/stop", summary="Full stop for a team's drive loop")
async def stop_team_drive_endpoint(
    team_id: str,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> DriveStopResult:
    """Kill switch: stop the runtime, drain the team inbox, cancel open tasks
    and waits. Idempotent — safe to call again."""
    _require_owned_team(server, user, team_id)
    repos = _repos(server)
    runtime = server.app_runtime
    autopilot = getattr(getattr(runtime, "gateway", None), "team_autopilot", None)
    if autopilot is not None:
        counts = await autopilot.stop(
            team_id,
            cron_manager=runtime.cron_manager if runtime is not None else None,
            team_inbox=runtime.agent_registry.team_inbox() if runtime is not None else None,
        )
    else:
        counts = await stop_team_drive(
            team_id=team_id,
            cron_manager=runtime.cron_manager if runtime is not None else None,
            task_repo=repos.team_task_repo,
            team_inbox=runtime.agent_registry.team_inbox() if runtime is not None else None,
        )
    return DriveStopResult(**counts)
