"""Deterministic application of a committed ``DecisionPlan`` (§6.2, phase 3).

The decision unit only plans; this module is the only writer that turns a
validated plan into board/ledger state. It never dispatches — the dispatcher
executes ready tasks after the plan is applied (docs/team-drive-v2.md §7.1).

Application is best-effort transactional: every op is validated up front
(``validate_decision_plan``) and applied in a stable order (goals → task
creates/deps → task mutations → waits). A mid-apply storage failure raises
``PlanApplyError``; the caller marks the decision failed and leaves the
ledger events for the next judgment.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from octop.infra.agents.teams.decision import DecisionPlan, validate_decision_plan
from octop.infra.agents.teams.task_state import GOAL_STATUSES, is_valid_task_transition
from octop.infra.db.repos.team_waits import (
    WAIT_KIND_APPROVAL,
    WAIT_KIND_TIMER,
    WAIT_STATUS_CANCELLED,
)

# Wait rows opened by a plan are given a conservative operation timeout; the
# policy itself is governed in phase 4.
DEFAULT_APPROVAL_TIMEOUT_SEC = 1800


class PlanApplyError(RuntimeError):
    """The plan could not be applied; the decision must be marked failed."""


@dataclass
class PlanApplyResult:
    goal_id: str | None = None
    created_tasks: dict[str, str] = field(default_factory=dict)
    applied_ops: list[str] = field(default_factory=list)
    notifications: list[str] = field(default_factory=list)


def _owner_id(repos: Any, team_id: str) -> int | None:
    row = repos.agent_repo.get(team_id)
    return int(row.user_id) if row is not None and row.user_id is not None else None


def _cancel_open_waits(repos: Any, task_id: str) -> None:
    for wait in repos.team_wait_repo.list_open_for_task(task_id):
        repos.team_wait_repo.settle(wait.wait_id, status=WAIT_STATUS_CANCELLED)


def _task_patch(repos: Any, task_id: str, fields: dict[str, object]) -> Any:
    row = repos.team_task_repo.get(task_id)
    if row is None:
        raise PlanApplyError(f"task not found: {task_id}")
    updated = repos.team_task_repo.update(task_id, expected_version=row.version, fields=fields)
    if updated is None:
        raise PlanApplyError(f"task {task_id} changed concurrently during plan apply")
    return updated


def _resolve_task(created: dict[str, str], key: str | None) -> str | None:
    if not key:
        return None
    return created.get(key, key)


def _apply_goal_ops(
    repos: Any,
    team_id: str,
    plan: DecisionPlan,
    *,
    decision_id: str,
    created_by: int | None,
    recorder: Any,
    result: PlanApplyResult,
) -> str | None:
    active = repos.team_goal_repo.active(team_id)
    for op in plan.goal_ops:
        actor = f"host:{decision_id}"
        if op.op == "update":
            if not op.goal_id or repos.team_goal_repo.get(op.goal_id) is None:
                raise PlanApplyError(f"unknown goal: {op.goal_id!r}")
            fields: dict[str, object] = {}
            if op.status is not None:
                if op.status not in GOAL_STATUSES:
                    raise PlanApplyError(f"invalid goal status: {op.status!r}")
                fields["status"] = op.status
            if op.title is not None:
                fields["title"] = op.title.strip()
            if op.success_criteria is not None:
                fields["success_criteria"] = op.success_criteria.strip()
            row = repos.team_goal_repo.update(op.goal_id, fields=fields)
            if row is None:
                raise PlanApplyError(f"goal not found: {op.goal_id!r}")
            active = row if row.status == "active" else active
            if recorder is not None:
                recorder.record_goal_change(row, actor=actor)
        else:
            title = (op.title or "").strip()
            if not title:
                raise PlanApplyError("goal set requires a title")
            repos.team_goal_repo.archive_active(team_id)
            row = repos.team_goal_repo.create(
                team_id=team_id,
                title=title,
                created_by=int(created_by or 0),
                description="",
                success_criteria=(op.success_criteria or "").strip(),
            )
            active = row
            if recorder is not None:
                recorder.record_goal_change(row, actor=actor)
        result.applied_ops.append(f"goal:{op.op}:{op.goal_id or row.goal_id}")
        if op.status in ("done", "archived", "paused"):
            active = None
    return active.goal_id if active is not None and active.status == "active" else None


def _apply_task_creates(
    repos: Any,
    team_id: str,
    plan: DecisionPlan,
    *,
    decision_id: str,
    goal_id: str | None,
    recorder: Any,
    result: PlanApplyResult,
) -> None:
    actor = f"host:{decision_id}"
    for op in plan.task_ops:
        if op.op != "create":
            continue
        local = op.task_id or ""
        idem = f"{decision_id}:{op.idempotency_key}"
        existing = repos.team_task_repo.get_by_idempotency_key(idem)
        if existing is not None:
            result.created_tasks[local] = existing.task_id
            continue
        row = repos.team_task_repo.create(
            team_id=team_id,
            title=(op.title or "").strip(),
            goal_id=goal_id,
            spec=(op.spec or "").strip(),
            assignee=op.assignee,
            priority=int(op.priority or 0),
            due_at=op.deadline_at,
            window_start=op.window_start,
            window_end=op.window_end,
            budget_tokens=op.budget_tokens,
            concurrency_key=op.concurrency_key,
            idempotency_key=idem,
        )
        result.created_tasks[local] = row.task_id
        result.applied_ops.append(f"task:create:{row.task_id}")
        if recorder is not None:
            recorder.record_task_change(row, actor=actor)

    # Dependencies are written after every create so forward references work.
    for op in plan.task_ops:
        if op.op != "create" or not op.depends_on:
            continue
        task_id = result.created_tasks.get(op.task_id or "")
        if not task_id:
            continue
        for dep in op.depends_on:
            dep_id = _resolve_task(result.created_tasks, dep)
            if dep_id is None or not repos.team_task_deps_repo.add(task_id, dep_id):
                continue
        result.applied_ops.append(f"task:deps:{task_id}")


def _apply_task_mutations(
    repos: Any,
    plan: DecisionPlan,
    *,
    decision_id: str,
    recorder: Any,
    result: PlanApplyResult,
) -> None:
    actor = f"host:{decision_id}"
    for op in plan.task_ops:
        if op.op == "create":
            continue
        task_id = _resolve_task(result.created_tasks, op.task_id)
        if not task_id:
            raise PlanApplyError(f"{op.op}: task_id required")
        row = repos.team_task_repo.get(task_id)
        if row is None:
            raise PlanApplyError(f"{op.op}: unknown task {task_id!r}")

        if op.op == "update":
            if op.expected_version is None:
                raise PlanApplyError(f"update {task_id}: expected_version required")
            fields = dict(op.fields or {})
            target = fields.get("status")
            if target is not None and not is_valid_task_transition(row.status, str(target)):
                raise PlanApplyError(
                    f"update {task_id}: illegal transition {row.status} -> {target}"
                )
            updated = repos.team_task_repo.update(
                task_id, expected_version=op.expected_version, fields=fields
            )
            if updated is None:
                raise PlanApplyError(f"update {task_id}: stale expected_version")
        elif op.op == "assign":
            fields = {}
            if op.assignee is not None:
                fields["assignee"] = op.assignee
            if op.concurrency_key is not None:
                fields["concurrency_key"] = op.concurrency_key
            if not fields:
                continue
            updated = _task_patch(repos, task_id, fields)
        elif op.op == "close":
            if not is_valid_task_transition(row.status, "done"):
                raise PlanApplyError(f"close {task_id}: illegal transition {row.status} -> done")
            updated = _task_patch(
                repos,
                task_id,
                {
                    "status": "done",
                    "result_summary": (op.result_summary or "").strip(),
                    "active_wait_id": None,
                },
            )
            _cancel_open_waits(repos, task_id)
        elif op.op == "cancel":
            if not is_valid_task_transition(row.status, "cancelled"):
                raise PlanApplyError(
                    f"cancel {task_id}: illegal transition {row.status} -> cancelled"
                )
            updated = _task_patch(
                repos,
                task_id,
                {
                    "status": "cancelled",
                    "blocked_reason": (op.reason or "").strip(),
                    "active_wait_id": None,
                },
            )
            _cancel_open_waits(repos, task_id)
        else:  # pragma: no cover - the pydantic pattern rejects this
            raise PlanApplyError(f"unknown task op: {op.op!r}")

        result.applied_ops.append(f"task:{op.op}:{task_id}")
        if recorder is not None:
            recorder.record_task_change(updated, actor=actor)


def _apply_wait_ops(
    repos: Any,
    team_id: str,
    plan: DecisionPlan,
    *,
    decision_id: str,
    recorder: Any,
    result: PlanApplyResult,
    now: int,
) -> None:
    actor = f"host:{decision_id}"
    for op in plan.wait_ops:
        task_id = _resolve_task(result.created_tasks, op.task_id)
        if op.op == "sleep":
            if op.until is None:
                raise PlanApplyError("sleep requires until")
            key = f"timer:{decision_id}:{op.idempotency_key}"
            wait = repos.team_wait_repo.get_by_idempotency_key(key)
            if wait is None:
                wait = repos.team_wait_repo.create(
                    team_id,
                    kind=WAIT_KIND_TIMER,
                    idempotency_key=key,
                    task_id=task_id,
                    due_at=int(op.until),
                    deadline_at=None,
                    payload={"decision_id": decision_id, "reason": op.reason or ""},
                )
            if wait is not None and task_id:
                _attach_wait(repos, task_id, wait.wait_id)
            result.applied_ops.append(f"wait:sleep:{task_id or team_id}")
        elif op.op == "wait_approval":
            key = f"approval:{decision_id}:{op.idempotency_key}"
            wait = repos.team_wait_repo.get_by_idempotency_key(key)
            if wait is None:
                wait = repos.team_wait_repo.create(
                    team_id,
                    kind=WAIT_KIND_APPROVAL,
                    idempotency_key=key,
                    task_id=task_id,
                    timeout_at=now + DEFAULT_APPROVAL_TIMEOUT_SEC,
                    payload={"decision_id": decision_id, "reason": op.reason or ""},
                )
            if wait is not None and task_id:
                _attach_wait(repos, task_id, wait.wait_id)
                row = repos.team_task_repo.get(task_id)
                if row is not None and is_valid_task_transition(row.status, "needs_input"):
                    updated = _task_patch(
                        repos,
                        task_id,
                        {
                            "status": "needs_input",
                            "blocked_reason": (op.reason or "waiting for creator approval").strip(),
                        },
                    )
                    if recorder is not None:
                        recorder.record_task_change(updated, actor=actor)
            result.notifications.append(
                "[creator] approval needed: "
                + (op.reason or "the team is waiting on your decision").strip()
            )
            result.applied_ops.append(f"wait:approval:{task_id or team_id}")
        elif op.op == "escalate":
            if task_id:
                row = repos.team_task_repo.get(task_id)
                if row is not None and is_valid_task_transition(row.status, "blocked"):
                    updated = _task_patch(
                        repos,
                        task_id,
                        {
                            "status": "blocked",
                            "blocked_reason": (op.reason or "escalated").strip(),
                            "active_wait_id": None,
                        },
                    )
                    if recorder is not None:
                        recorder.record_task_change(updated, actor=actor)
            target = op.to or "creator"
            result.notifications.append(f"[{target}] {op.reason or 'team needs a decision'}")
            result.applied_ops.append(f"wait:escalate:{task_id or team_id}")
        else:  # pragma: no cover - pydantic rejects unknown ops
            raise PlanApplyError(f"unknown wait op: {op.op!r}")


def _attach_wait(repos: Any, task_id: str, wait_id: str) -> None:
    row = repos.team_task_repo.get(task_id)
    if row is None or row.active_wait_id == wait_id:
        return
    repos.team_task_repo.update(
        task_id,
        expected_version=row.version,
        fields={"active_wait_id": wait_id},
    )


def apply_decision_plan(
    repos: Any,
    team_id: str,
    plan: DecisionPlan,
    *,
    decision_id: str,
    created_by: int | None = None,
    recorder: Any | None = None,
    member_ids: set[str] | None = None,
    now: int | None = None,
) -> PlanApplyResult:
    """Apply a validated ``DecisionPlan``. Raises ``PlanApplyError`` on failure.

    ``created_by`` defaults to the team owner (goal rows FK to users).
    ``recorder`` is the coordinator; every board mutation is written to the
    event ledger with ``actor=host:<decision_id>`` so it cannot self-trigger.
    """
    violations = validate_decision_plan(plan, repos, team_id, member_ids=member_ids)
    if violations:
        raise PlanApplyError("; ".join(violations))
    if plan.noop:
        return PlanApplyResult()

    now = int(time.time()) if now is None else int(now)
    owner = _owner_id(repos, team_id) if created_by is None else int(created_by)
    if any(op.op == "set" for op in plan.goal_ops) and not owner:
        raise PlanApplyError("goal set requires a team owner")
    result = PlanApplyResult()
    goal_id = _apply_goal_ops(
        repos,
        team_id,
        plan,
        decision_id=decision_id,
        created_by=owner,
        recorder=recorder,
        result=result,
    )
    _apply_task_creates(
        repos,
        team_id,
        plan,
        decision_id=decision_id,
        goal_id=goal_id,
        recorder=recorder,
        result=result,
    )
    _apply_task_mutations(
        repos,
        plan,
        decision_id=decision_id,
        recorder=recorder,
        result=result,
    )
    _apply_wait_ops(
        repos,
        team_id,
        plan,
        decision_id=decision_id,
        recorder=recorder,
        result=result,
        now=now,
    )
    if plan.goal_ops:
        runtime = repos.team_runtime_repo.get(team_id)
        if runtime is not None:
            repos.team_runtime_repo.update(
                team_id,
                expected_version=runtime.version,
                fields={"goal_id": goal_id},
            )
    if plan.notify is not None and plan.notify.text.strip():
        result.notifications.append(plan.notify.text.strip())
    result.goal_id = goal_id
    return result


__all__ = [
    "DEFAULT_APPROVAL_TIMEOUT_SEC",
    "PlanApplyError",
    "PlanApplyResult",
    "apply_decision_plan",
]
