"""Built-in goal/task tools mounted on team hosts only.

Thin wrappers over the board repos: every write lands in ``team_goals`` /
``team_tasks`` and returns JSON the host can act on. Room visibility comes from
the host's own reply; structured room events and WS board events arrive with
the P2 drive loop / P4 board (see docs/team-autopilot-design.html §7.2).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any
from urllib.parse import urlparse

from langchain_core.tools import StructuredTool
from langgraph.config import get_config
from pydantic import Field

from octop.infra.agents.teams.drive import TEAM_DRIVE_OPEN_TASK_LIMIT
from octop.infra.agents.teams.task_state import TASK_STATUSES, is_valid_task_transition
from octop.infra.agents.threads.artifact import artifact_path_allowed
from octop.infra.agents.tool_scope import current_tool_scope
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.apps import LightAppRepo
from octop.infra.db.repos.goal_deliverables import GoalDeliverableRepo
from octop.infra.db.repos.team_goals import TeamGoalRepo
from octop.infra.db.repos.team_runtime import TeamRuntimeRepo
from octop.infra.db.repos.team_tasks import TeamTaskRepo
from octop.infra.db.repos.team_waits import WAIT_STATUS_CANCELLED, TeamWaitRepo

GOAL_SET = "goal_set"
TASK_CREATE = "task_create"
TASK_UPDATE = "task_update"
TASK_LIST = "task_list"
DELIVERABLE_UPDATE = "deliverable_update"

TEAM_BOARD_TOOL_NAMES: tuple[str, ...] = (
    GOAL_SET,
    TASK_CREATE,
    TASK_UPDATE,
    TASK_LIST,
    DELIVERABLE_UPDATE,
)
"""Names of the built-in team board tools, in the order they are registered."""

_TASK_LIST_LIMIT = 100

CLEARABLE_TASK_FIELDS: tuple[str, ...] = ("due_at", "blocked_reason", "result_summary")
"""Fields ``task_update(clear_fields=...)`` may reset to empty — a stale
deadline must be clearable or scheduling keeps re-triggering on it."""

_CLEAR_FIELDS_DESCRIPTION = (
    "Field names to reset to empty: " + ", ".join(CLEARABLE_TASK_FIELDS) + "."
)

TaskNotify = Callable[[int, dict[str, Any]], Awaitable[None]]
"""Async WS push (user_id, frame) — wired to ``WebSocketHub.push_to_user``."""


def task_updated_frame(task: Any) -> dict[str, Any]:
    """Minimal §7.5 board event — the dashboard refetches the board on receipt."""
    return {
        "type": "team.task.updated",
        "team_id": task.team_id,
        "task_id": task.task_id,
        "status": task.status,
        "version": task.version,
    }


def _tool_ctx() -> tuple[str, int]:
    scope = current_tool_scope()
    if scope is not None:
        return scope
    cfg = get_config().get("configurable") or {}
    agent_id = cfg.get("agent_id")
    user_raw = cfg.get("user")
    if not agent_id:
        raise ValueError("missing configurable.agent_id")
    if user_raw is None:
        raise ValueError("missing configurable.user")
    return str(agent_id), int(user_raw)


def _ok(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _err(message: str, **details: Any) -> str:
    payload: dict[str, Any] = {"error": message}
    payload.update(details)
    return _ok(payload)


def _goal_summary(row: Any) -> dict[str, Any]:
    return {
        "goal_id": row.goal_id,
        "team_id": row.team_id,
        "title": row.title,
        "description": row.description,
        "status": row.status,
        "success_criteria": row.success_criteria,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _task_summary(row: Any) -> dict[str, Any]:
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


def build_team_board_tools(
    *,
    goal_repo: TeamGoalRepo,
    task_repo: TeamTaskRepo,
    deliverable_repo: GoalDeliverableRepo | None = None,
    light_app_repo: LightAppRepo | None = None,
    agent_repo: AgentRepo,
    task_notify: TaskNotify | None = None,
    event_recorder: Any | None = None,
    wait_repo: TeamWaitRepo | None = None,
    runtime_repo: TeamRuntimeRepo | None = None,
    self_only: bool = False,
    member_ids_fn: Callable[[str], set[str]] | None = None,
) -> list[StructuredTool]:
    """Return built-in team board tools (wired via ``HarnessAgentConfig.tools``).

    ``event_recorder`` is the shadow coordinator (docs/team-drive-v2.md
    §11.2 phase 1): successful writes append a ledger event on the side.

    ``member_ids_fn`` supplies the roster for assignee validation. The
    decision path already validates against the roster; without this the
    tool path accepted any agent id in the system, so a host (or a prompt
    injected into its room) could hand this team's work to a stranger.
    """

    def _require_host(agent_id: str, user_id: int) -> Any:
        row = agent_repo.get(agent_id)
        if row is None or row.user_id is None or int(row.user_id) != user_id:
            raise ValueError("team board tools are only available to the team creator")
        return row

    def _assignee_error(agent_id: str, assignee: str) -> str | None:
        """Reject an assignee outside this team's roster (None = ok)."""
        if self_only:
            if assignee != agent_id:
                return "autonomous agents may only assign tasks to themselves"
            return None
        if member_ids_fn is None:
            return None
        try:
            roster = set(member_ids_fn(agent_id))
        except Exception:
            # An unreadable roster must not silently widen the check.
            return "team roster is unavailable; cannot validate assignee"
        if assignee not in roster:
            return f"assignee is not a member of this team: {assignee}"
        return None

    def _app_error(host_id: str, app_id: str) -> str | None:
        """Reject an app artifact outside this team / digital employee."""
        if light_app_repo is None:
            return "MiniApp ledger is not available"
        app = light_app_repo.get(app_id)
        if app is None:
            return f"MiniApp not found: {app_id}"
        if self_only:
            if app.agent_id != host_id:
                return "autonomous agents may only deliver their own apps"
            return None
        allowed = {host_id}
        if member_ids_fn is not None:
            try:
                allowed |= set(member_ids_fn(host_id))
            except Exception:
                return "team roster is unavailable; cannot validate app"
        if app.agent_id not in allowed:
            return "MiniApp is not hosted by this team"
        return None

    async def goal_set(
        title: str = Field(..., description="Goal title, one line."),
        description: str = Field("", description="Goal background and constraints."),
        success_criteria: str = Field("", description="What done looks like."),
    ) -> str:
        try:
            agent_id, user_id = _tool_ctx()
            _require_host(agent_id, user_id)
            goal_repo.archive_active(agent_id)
            row = goal_repo.create(
                team_id=agent_id,
                title=title.strip(),
                created_by=user_id,
                description=description.strip(),
                success_criteria=success_criteria.strip(),
            )
            if task_notify is not None:
                await task_notify(
                    user_id,
                    {
                        "type": "team.goal.updated",
                        "team_id": agent_id,
                        "goal_id": row.goal_id,
                        "status": row.status,
                    },
                )
            if event_recorder is not None:
                event_recorder.record_goal_change(row, actor="host")
            if runtime_repo is not None:
                runtime = runtime_repo.ensure(agent_id)
                runtime_repo.update(
                    agent_id,
                    expected_version=runtime.version,
                    fields={"goal_id": row.goal_id},
                )
            return _ok({"goal": _goal_summary(row)})
        except Exception as exc:
            return _err(str(exc))

    async def task_create(
        title: str = Field(..., description="Task title, one line."),
        spec: str = Field("", description="Self-contained assignment brief for the assignee."),
        goal_id: str | None = Field(None, description="Optional goal this task serves."),
        assignee: str | None = Field(None, description="Optional member agent_id to assign."),
        priority: int = Field(0, description="Higher runs first. 0 is default."),
        due_at: int | None = Field(None, description="Optional unix timestamp deadline."),
    ) -> str:
        try:
            agent_id, user_id = _tool_ctx()
            _require_host(agent_id, user_id)
            if task_repo.count_open(agent_id) >= TEAM_DRIVE_OPEN_TASK_LIMIT:
                return _err(
                    f"open task limit reached ({TEAM_DRIVE_OPEN_TASK_LIMIT}); "
                    "finish or cancel open tasks before creating more"
                )
            if goal_id is not None:
                goal = goal_repo.get(goal_id)
                if goal is None or goal.team_id != agent_id:
                    return _err(f"goal not found: {goal_id}")
            if self_only and assignee is None:
                # A digital employee is its own executor: leaving the task
                # unassigned would make the dispatcher escalate it as
                # "no assignee" and the row would never run.
                assignee = agent_id
            if assignee is not None:
                problem = _assignee_error(agent_id, assignee)
                if problem is not None:
                    return _err(problem, agent_id=agent_id, assignee=assignee)
                member = agent_repo.get(assignee)
                if member is None:
                    return _err(f"assignee not found: {assignee}")
            row = task_repo.create(
                team_id=agent_id,
                title=title.strip(),
                goal_id=goal_id,
                spec=spec.strip(),
                assignee=assignee,
                priority=int(priority),
                due_at=due_at,
            )
            if task_notify is not None:
                await task_notify(user_id, task_updated_frame(row))
            if event_recorder is not None:
                event_recorder.record_task_change(row, actor="host")
            return _ok({"task": _task_summary(row)})
        except Exception as exc:
            return _err(str(exc))

    async def task_update(
        task_id: str = Field(..., description="Task to update (from task_create / task_list)."),
        status: str | None = Field(
            None,
            description="New status. Legal: " + ", ".join(sorted(TASK_STATUSES)) + ".",
        ),
        result_summary: str | None = Field(None, description="Outcome summary on done/failed."),
        blocked_reason: str | None = Field(None, description="Why blocked / what input is needed."),
        assignee: str | None = Field(None, description="Member agent_id to (re)assign."),
        priority: int | None = Field(None, description="Higher runs first."),
        due_at: int | None = Field(None, description="Unix timestamp deadline."),
        clear_fields: Sequence[str] | None = Field(None, description=_CLEAR_FIELDS_DESCRIPTION),
    ) -> str:
        try:
            agent_id, user_id = _tool_ctx()
            _require_host(agent_id, user_id)
            current = task_repo.get(task_id)
            if current is None or current.team_id != agent_id:
                return _err(f"task not found: {task_id}")
            if status is not None and not is_valid_task_transition(current.status, status):
                return _err(
                    f"illegal transition {current.status} -> {status}",
                    from_status=current.status,
                    to_status=status,
                )
            fields: dict[str, object] = {}
            for name in clear_fields or []:
                if name not in CLEARABLE_TASK_FIELDS:
                    return _err(
                        f"field not clearable: {name}",
                        clearable=", ".join(CLEARABLE_TASK_FIELDS),
                    )
                fields[name] = None
            if status is not None:
                fields["status"] = status
            if result_summary is not None:
                fields["result_summary"] = result_summary.strip()
            if blocked_reason is not None:
                fields["blocked_reason"] = blocked_reason.strip()
            if assignee is not None:
                problem = _assignee_error(agent_id, assignee)
                if problem is not None:
                    return _err(problem, agent_id=agent_id, assignee=assignee)
                if agent_repo.get(assignee) is None:
                    return _err(f"assignee not found: {assignee}")
                fields["assignee"] = assignee
            if priority is not None:
                fields["priority"] = int(priority)
            if due_at is not None:
                fields["due_at"] = int(due_at)
            terminal = status in ("done", "failed", "cancelled")
            if terminal:
                fields["active_wait_id"] = None
            updated = task_repo.update(task_id, expected_version=current.version, fields=fields)
            if updated is None:
                return _err("task was modified concurrently; retry with fresh task_list")
            if terminal and wait_repo is not None:
                for wait in wait_repo.list_open_for_task(task_id):
                    wait_repo.settle(wait.wait_id, status=WAIT_STATUS_CANCELLED)
            if task_notify is not None:
                await task_notify(user_id, task_updated_frame(updated))
            if event_recorder is not None:
                event_recorder.record_task_change(updated, actor="host")
            return _ok({"task": _task_summary(updated)})
        except Exception as exc:
            return _err(str(exc))

    async def deliverable_update(
        deliverable_id: str = Field(..., description="Deliverable id from goal/deliverable views."),
        status: str = Field("produced", description="pending or produced."),
        artifact_type: str = Field(
            "none",
            description="none, file (workspace path), link (http/https), text, or app (MiniApp id).",
        ),
        artifact_ref: str = Field("", description="Workspace path, URL, or text body."),
        artifact_name: str = Field("", description="Display name for the artifact."),
        artifact_agent_id: str | None = Field(
            None, description="Agent whose workspace owns a file artifact; defaults to host."
        ),
    ) -> str:
        """Mark a goal deliverable produced and attach its actual artifact."""
        try:
            if deliverable_repo is None:
                return _err("deliverable ledger is not available")
            agent_id, user_id = _tool_ctx()
            _require_host(agent_id, user_id)
            current = deliverable_repo.get(deliverable_id)
            if current is None:
                return _err(f"deliverable not found: {deliverable_id}")
            goal = goal_repo.get(current.goal_id)
            if goal is None or goal.team_id != agent_id:
                return _err(f"deliverable not found: {deliverable_id}")
            if status not in ("pending", "produced"):
                return _err(f"invalid deliverable status: {status}")
            if artifact_type not in ("none", "file", "link", "text", "app"):
                return _err(f"invalid artifact type: {artifact_type}")
            fields: dict[str, object] = {"status": status}
            if artifact_type == "none":
                if status == "produced":
                    return _err("produced deliverables need a file, link, text or app artifact")
                fields.update(
                    artifact_type="none",
                    artifact_ref="",
                    artifact_name="",
                    artifact_agent_id=None,
                )
            else:
                ref = artifact_ref.strip()
                if not ref:
                    return _err("artifact reference is required")
                if artifact_type == "file":
                    if not artifact_path_allowed(ref):
                        return _err("artifact path is not allowed")
                    aid = (artifact_agent_id or agent_id).strip()
                    target = agent_repo.get(aid)
                    if target is None or (int(target.user_id or 0) != user_id and aid != agent_id):
                        return _err("artifact agent is not owned by the host")
                    fields["artifact_agent_id"] = aid
                elif artifact_type == "link":
                    parsed = urlparse(ref)
                    if parsed.scheme not in ("http", "https") or not parsed.netloc:
                        return _err("artifact link must be http(s)")
                    fields["artifact_agent_id"] = None
                elif artifact_type == "app":
                    app_error = _app_error(agent_id, ref)
                    if app_error is not None:
                        return _err(app_error)
                    fields["artifact_agent_id"] = None
                else:
                    if len(ref) > 4000:
                        return _err("artifact text is too long")
                    fields["artifact_agent_id"] = None
                fields.update(
                    artifact_type=artifact_type,
                    artifact_ref=ref,
                    artifact_name=artifact_name.strip() or ref[:200],
                )
            updated = deliverable_repo.update(deliverable_id, fields=fields)
            if updated is None:
                return _err("deliverable changed concurrently; retry")
            if task_notify is not None:
                await task_notify(
                    user_id,
                    {
                        "type": "team.goal.updated",
                        "team_id": agent_id,
                        "goal_id": updated.goal_id,
                        "deliverable_id": updated.deliverable_id,
                    },
                )
            return _ok(
                {
                    "deliverable_id": updated.deliverable_id,
                    "status": updated.status,
                    "artifact_type": updated.artifact_type,
                    "artifact_ref": updated.artifact_ref,
                    "artifact_agent_id": updated.artifact_agent_id,
                }
            )
        except Exception as exc:
            return _err(str(exc))

    async def task_list(
        status: str | None = Field(None, description="Filter by one status."),
        goal_id: str | None = Field(None, description="Filter by goal."),
    ) -> str:
        try:
            agent_id, user_id = _tool_ctx()
            _require_host(agent_id, user_id)
            rows = task_repo.list_by_team(agent_id, status=status, goal_id=goal_id)
            return _ok(
                {
                    "open_count": task_repo.count_open(agent_id),
                    "tasks": [_task_summary(row) for row in rows[:_TASK_LIST_LIMIT]],
                }
            )
        except Exception as exc:
            return _err(str(exc))

    return [
        StructuredTool.from_function(
            coroutine=goal_set,
            name=GOAL_SET,
            description=(
                "Set (or replace) this team's single active goal. Archiving the "
                "previous goal keeps the team focused on one north star."
            ),
        ),
        StructuredTool.from_function(
            coroutine=task_create,
            name=TASK_CREATE,
            description=(
                "Create a task for the team board. Break the active goal into "
                "concrete, assignable tasks; dispatch members with ask_agent "
                "afterwards, then keep status current via task_update."
            ),
        ),
        StructuredTool.from_function(
            coroutine=task_update,
            name=TASK_UPDATE,
            description=(
                "Update a task's status/fields as work progresses. Call this "
                "when a member finishes, fails, or gets blocked (their reply "
                "arrives via ask_agent callbacks)."
            ),
        ),
        StructuredTool.from_function(
            coroutine=task_list,
            name=TASK_LIST,
            description="List this team's tasks with open-task count.",
        ),
        StructuredTool.from_function(
            coroutine=deliverable_update,
            name=DELIVERABLE_UPDATE,
            description=(
                "Mark a goal deliverable produced and attach the actual workspace "
                "file, link, or text artifact. Use the deliverable ids from the "
                "goal view; never invent them."
            ),
        ),
    ]
