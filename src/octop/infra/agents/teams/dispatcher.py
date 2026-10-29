"""Team dispatcher: projection (§7.1, phase 2) and execution (phase 3).

``plan_dispatches`` is a pure projection of what the dispatcher would do with
the current board — used by the shadow decision unit and the dashboard. The
``TeamDispatcher`` class executes the same algorithm: it opens a
``member_reply`` wait before calling the execution adapter, marks the task
``assigned → running``, and never holds a lock across a member run. Callers
(coordinator/sweeper) provide the adapter callback; this module contains no
harness, HTTP, or LLM code.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from octop.infra.agents.teams.task_state import OPEN_TASK_STATUSES, is_valid_task_transition
from octop.infra.db.repos.team_waits import (
    WAIT_KIND_MEMBER_REPLY,
    WAIT_KIND_TIMER,
    WAIT_STATUS_CANCELLED,
    WAIT_STATUS_TIMED_OUT,
)

logger = logging.getLogger(__name__)

# Terminal dependency states: anything else blocks the dependent task.
_DEP_OPEN = OPEN_TASK_STATUSES - {"done"}
_DISPATCH_PROJECTION_LIMIT = 500
_DEFAULT_DISPATCH_TIMEOUT_SEC = 3600
_DISPATCHABLE_STATUSES = frozenset({"proposed"})
_RUNTIME_DISPATCH_STATUSES = frozenset({"running", "waiting", "dispatching"})


@dataclass(frozen=True)
class DispatchStep:
    """One line of the dispatch projection, in planned order."""

    task_id: str
    title: str
    assignee: str | None
    action: str  # dispatch | wait_window | escalate_deadline | escalate_deps | defer_budget | skip_deps | skip_busy | skip_wait | skip_status | defer_limit
    reason: str


@dataclass(frozen=True)
class DispatchTarget:
    """Where a member's streamed run lands: the team's active room."""

    team_id: str
    user_id: int
    thread_id: str
    session_key: str


@dataclass
class DispatchOutcome:
    task_id: str
    action: str
    status: str  # dispatched | skipped | escalated | failed
    reason: str = ""
    wait_id: str | None = None
    job_id: str | None = None


SubmitPeer = Callable[[str, str, str, DispatchTarget], Awaitable[str | None]]
"""Adapter callback: (team_id, member_id, message, target) -> inbox job id."""

ResolveTarget = Callable[[str], Awaitable[DispatchTarget | None]]
"""Resolve the team's current room; ``None`` means no dispatch is possible."""


def budget_remaining(repos: Any, team_id: str) -> int | None:
    """Tokens this team may still spend today (``None`` = no limit).

    The limit lives on the autonomy policy row; a team host or digital
    employee without one is unlimited, which is the pre-budget behaviour.
    """
    runtime = repos.team_runtime_repo.get(team_id)
    # Lightweight repo bundles (offline CLI, unit fakes) may not carry the
    # policy repo; no policy means no limit, which is the pre-budget default.
    policy_repo = getattr(repos, "autonomy_policy_repo", None)
    if policy_repo is None:
        return None
    policy = policy_repo.get(team_id)
    limit = int(policy.budget_limit_tokens or 0)
    if limit <= 0:
        return None
    used = int(runtime.budget_used) if runtime is not None else 0
    return max(0, limit - used)


def task_sort_key(task: Any) -> tuple[int, int, int]:
    deadline = task.due_at if task.due_at is not None else (1 << 62)
    return (-task.priority, 1 if task.due_at is None else 0, deadline)


def plan_dispatches(
    repos: Any,
    team_id: str,
    *,
    now: int | None = None,
    max_dispatches: int = 5,
    budget_remaining: int | None = None,
) -> list[DispatchStep]:
    """Project the §7.1 dispatch order for ``team_id`` without side effects.

    ``budget_remaining`` is injected (None disables the budget gate) — the
    team daily budget itself is governed in phase 4.
    """
    now = now if now is not None else int(time.time())
    rows = repos.team_task_repo.list_by_team(team_id, limit=_DISPATCH_PROJECTION_LIMIT)
    status_of = {t.task_id: t.status for t in rows}
    open_rows = sorted((t for t in rows if t.status in OPEN_TASK_STATUSES), key=task_sort_key)

    dep_map: dict[str, list[str]] = {}
    for edge in repos.team_task_deps_repo.list_for_team(team_id):
        dep_map.setdefault(edge.task_id, []).append(edge.depends_on_task_id)

    busy_keys: set[str] = set()
    for t in open_rows:
        if t.status in ("assigned", "running"):
            busy_keys.add(t.concurrency_key or t.assignee or t.task_id)

    steps: list[DispatchStep] = []
    dispatched = 0
    for task in open_rows:
        deps = dep_map.get(task.task_id, [])
        blocked = [d for d in deps if status_of.get(d) in _DEP_OPEN]
        broken = [d for d in deps if status_of.get(d) in ("failed", "cancelled")]
        if broken:
            steps.append(
                DispatchStep(
                    task.task_id,
                    task.title,
                    task.assignee,
                    "escalate_deps",
                    f"dependency failed: {', '.join(broken)}",
                )
            )
            continue
        if blocked:
            steps.append(
                DispatchStep(
                    task.task_id,
                    task.title,
                    task.assignee,
                    "skip_deps",
                    f"waiting on: {', '.join(blocked)}",
                )
            )
            continue
        if task.active_wait_id:
            steps.append(
                DispatchStep(
                    task.task_id,
                    task.title,
                    task.assignee,
                    "skip_wait",
                    f"blocked on wait {task.active_wait_id}",
                )
            )
            continue
        if task.status not in _DISPATCHABLE_STATUSES and task.status not in ("assigned", "running"):
            steps.append(
                DispatchStep(
                    task.task_id,
                    task.title,
                    task.assignee,
                    "skip_status",
                    f"status {task.status}",
                )
            )
            continue
        key = task.concurrency_key or task.assignee or task.task_id
        if key in busy_keys:
            steps.append(
                DispatchStep(
                    task.task_id,
                    task.title,
                    task.assignee,
                    "skip_busy",
                    f"concurrency key busy: {key}",
                )
            )
            continue
        if task.window_start is not None and now < task.window_start:
            steps.append(
                DispatchStep(
                    task.task_id,
                    task.title,
                    task.assignee,
                    "wait_window",
                    f"window opens at {task.window_start}",
                )
            )
            continue
        if task.due_at is not None and now > task.due_at:
            steps.append(
                DispatchStep(
                    task.task_id, task.title, task.assignee, "escalate_deadline", "deadline_missed"
                )
            )
            continue
        if budget_remaining is not None and (task.budget_tokens or 0) > budget_remaining:
            steps.append(
                DispatchStep(
                    task.task_id,
                    task.title,
                    task.assignee,
                    "defer_budget",
                    f"needs {task.budget_tokens or 0}, remaining {budget_remaining}",
                )
            )
            continue
        if dispatched >= max_dispatches:
            steps.append(
                DispatchStep(
                    task.task_id,
                    task.title,
                    task.assignee,
                    "defer_limit",
                    f"max_dispatches={max_dispatches}",
                )
            )
            continue
        dispatched += 1
        steps.append(
            DispatchStep(
                task.task_id,
                task.title,
                task.assignee,
                "dispatch",
                f"assignee={task.assignee or 'auto'}",
            )
        )
    return steps


class TeamDispatcher:
    """Execute ready tasks through the harness inbox (§7.1).

    No member run is awaited here: ``submit_peer`` enqueues and returns a job
    id immediately. The corresponding ``member_reply`` wait is settled by the
    coordinator when the inbox calls ``on_reply``.
    """

    def __init__(
        self,
        *,
        repos: Any,
        resolve_target: ResolveTarget,
        submit_peer: SubmitPeer,
        recorder: Any | None = None,
        dispatch_timeout_sec: int = _DEFAULT_DISPATCH_TIMEOUT_SEC,
        max_dispatches: int = 5,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._repos = repos
        self._resolve_target = resolve_target
        self._submit_peer = submit_peer
        self._recorder = recorder
        self._dispatch_timeout_sec = int(dispatch_timeout_sec)
        self._max_dispatches = int(max_dispatches)
        self._clock = clock

    def bind_recorder(self, recorder: Any | None) -> None:
        self._recorder = recorder

    def replace_repos(self, repos: Any) -> None:
        """Rebind after a control-plane database hot-swap."""
        self._repos = repos

    async def dispatch_ready(
        self,
        team_id: str,
        *,
        now: int | None = None,
        max_dispatches: int | None = None,
    ) -> list[DispatchOutcome]:
        """Dispatch ready tasks once; returns one outcome per projected step."""
        runtime_repo = self._repos.team_runtime_repo
        runtime = runtime_repo.get(team_id) or runtime_repo.ensure(team_id)
        if runtime.mode != "autopilot" or runtime.status not in _RUNTIME_DISPATCH_STATUSES:
            return []

        now = int(self._clock()) if now is None else int(now)
        try:
            target = await self._resolve_target(team_id)
        except Exception:
            logger.warning("team %s dispatch target resolution failed", team_id, exc_info=True)
            target = None
        if target is None:
            return []

        steps = plan_dispatches(
            self._repos,
            team_id,
            now=now,
            max_dispatches=max_dispatches or self._max_dispatches,
            budget_remaining=budget_remaining(self._repos, team_id),
        )
        outcomes: list[DispatchOutcome] = []
        for step in steps:
            current = runtime_repo.get(team_id)
            if current is None or current.status not in _RUNTIME_DISPATCH_STATUSES:
                break  # paused/stopped mid-pass
            outcome = await self._execute_step(team_id, step, target=target, now=now)
            if outcome is not None:
                outcomes.append(outcome)
        self._refresh_status(team_id)
        return outcomes

    async def _execute_step(
        self,
        team_id: str,
        step: DispatchStep,
        *,
        target: DispatchTarget,
        now: int,
    ) -> DispatchOutcome | None:
        repos = self._repos
        task = repos.team_task_repo.get(step.task_id)
        if task is None:
            return DispatchOutcome(step.task_id, step.action, "skipped", "task disappeared")

        if step.action == "wait_window":
            self._open_timer_wait(
                team_id,
                task,
                due_at=int(task.window_start or now),
                key=f"timer:{task.task_id}:window:{task.window_start}",
                reason="window",
            )
            return DispatchOutcome(step.task_id, step.action, "skipped", step.reason)
        if step.action in ("escalate_deadline", "escalate_deps"):
            return self._escalate(team_id, task, step)
        if step.action != "dispatch":
            return DispatchOutcome(step.task_id, step.action, "skipped", step.reason)

        if task.status not in _DISPATCHABLE_STATUSES:
            return DispatchOutcome(step.task_id, step.action, "skipped", f"status {task.status}")
        if not task.assignee:
            escalated = repos.team_task_repo.update(
                task.task_id,
                expected_version=task.version,
                fields={"status": "blocked", "blocked_reason": "no assignee; needs a decision"},
            )
            if escalated is not None and self._recorder is not None:
                self._recorder.record_task_change(escalated, actor="coordinator")
            self._record_escalation(team_id, task.task_id, "no_assignee", version=task.version)
            return DispatchOutcome(step.task_id, step.action, "escalated", "no assignee")

        attempt = repos.team_wait_repo.count_for_task(task.task_id, kind=WAIT_KIND_MEMBER_REPLY) + 1
        timeout_at = now + self._dispatch_timeout_sec
        if task.due_at is not None:
            # The business deadline is a hard stop even if the member keeps going.
            timeout_at = min(timeout_at, max(now, task.due_at))
        wait = repos.team_wait_repo.create(
            team_id,
            kind=WAIT_KIND_MEMBER_REPLY,
            idempotency_key=f"dispatch:{task.task_id}:{attempt}",
            task_id=task.task_id,
            timeout_at=timeout_at,
            deadline_at=task.due_at,
            escalation_policy={"on_timeout": "judgment", "reason": "member_reply_timeout"},
            payload={
                "message": task.spec or task.title,
                "source_thread_id": target.thread_id,
                "session_key": target.session_key,
                "mode": "autopilot",
                "attempt": attempt,
            },
        )
        if wait is None:
            return DispatchOutcome(step.task_id, step.action, "skipped", "dispatch wait exists")
        self._notify_wait(wait)

        assigned = repos.team_task_repo.update(
            task.task_id,
            expected_version=task.version,
            fields={
                "status": "assigned",
                "active_wait_id": wait.wait_id,
                "concurrency_key": task.concurrency_key or task.assignee,
            },
        )
        if assigned is None:
            repos.team_wait_repo.settle(wait.wait_id, status=WAIT_STATUS_CANCELLED)
            self._notify_wait(wait.wait_id)
            return DispatchOutcome(
                step.task_id, step.action, "skipped", "task changed concurrently"
            )

        try:
            job_id = await self._submit_peer(
                team_id,
                str(task.assignee),
                task.spec or task.title,
                target,
            )
        except Exception as exc:
            logger.warning("team %s dispatch to %s failed", team_id, task.assignee, exc_info=True)
            repos.team_wait_repo.settle(wait.wait_id, status=WAIT_STATUS_TIMED_OUT)
            self._notify_wait(wait.wait_id)
            # A dispatch failure is recoverable, so park the task instead of
            # killing it: ``failed`` is terminal and the decision unit would
            # never touch the task again, which defeats the escalation event
            # recorded below. It is also not a legal edge from ``assigned``
            # in the shared state machine (INV-2).
            failed = (
                repos.team_task_repo.update(
                    task.task_id,
                    expected_version=assigned.version,
                    fields={
                        "status": "blocked",
                        "blocked_reason": f"dispatch failed: {type(exc).__name__}: {exc}",
                        "active_wait_id": None,
                    },
                )
                if is_valid_task_transition(assigned.status, "blocked")
                else None
            )
            if failed is not None and self._recorder is not None:
                self._recorder.record_task_change(failed, actor="coordinator")
            self._record_escalation(
                team_id, task.task_id, "dispatch_failed", version=assigned.version
            )
            return DispatchOutcome(
                step.task_id,
                step.action,
                "failed",
                f"{type(exc).__name__}: {exc}",
                wait_id=wait.wait_id,
            )

        if job_id:
            repos.team_wait_repo.set_external_ref(wait.wait_id, job_id)
        running = repos.team_task_repo.update(
            task.task_id,
            expected_version=assigned.version,
            fields={"status": "running"},
        )
        if running is None:
            # The task changed under us (user cancel/edit); do not leave the
            # inbox job parked on a wait nobody will settle.
            repos.team_wait_repo.settle(wait.wait_id, status=WAIT_STATUS_CANCELLED)
            self._notify_wait(wait.wait_id)
            return DispatchOutcome(
                step.task_id, step.action, "skipped", "task changed after submit"
            )
        if self._recorder is not None:
            self._recorder.record_task_change(running, actor="coordinator")
        return DispatchOutcome(
            step.task_id,
            step.action,
            "dispatched",
            step.reason,
            wait_id=wait.wait_id,
            job_id=job_id,
        )

    def _open_timer_wait(
        self,
        team_id: str,
        task: Any,
        *,
        due_at: int,
        key: str,
        reason: str,
    ) -> None:
        wait = self._repos.team_wait_repo.get_by_idempotency_key(key)
        if wait is None:
            wait = self._repos.team_wait_repo.create(
                team_id,
                kind=WAIT_KIND_TIMER,
                idempotency_key=key,
                task_id=task.task_id,
                due_at=due_at,
                payload={"reason": reason, "task_id": task.task_id},
            )
        if wait is not None:
            self._notify_wait(wait)
        if wait is not None and task.active_wait_id != wait.wait_id:
            self._repos.team_task_repo.update(
                task.task_id,
                expected_version=task.version,
                fields={"active_wait_id": wait.wait_id},
            )

    def _escalate(self, team_id: str, task: Any, step: DispatchStep) -> DispatchOutcome:
        reason = step.reason or step.action
        row = self._repos.team_task_repo.get(task.task_id)
        updated = None
        if row is not None and row.status in _DISPATCHABLE_STATUSES:
            updated = self._repos.team_task_repo.update(
                task.task_id,
                expected_version=row.version,
                fields={"status": "blocked", "blocked_reason": reason, "active_wait_id": None},
            )
            if updated is not None and self._recorder is not None:
                self._recorder.record_task_change(updated, actor="coordinator")
        version = updated.version if updated is not None else (row.version if row else 0)
        self._record_escalation(team_id, task.task_id, step.action, version=version)
        return DispatchOutcome(task.task_id, step.action, "escalated", reason)

    def _notify_wait(self, wait_or_id: Any) -> None:
        notifier = getattr(self._recorder, "notify_wait", None)
        if not callable(notifier):
            return
        if isinstance(wait_or_id, str):
            row = self._repos.team_wait_repo.get(wait_or_id)
        else:
            row = wait_or_id
        if row is not None:
            notifier(row)

    def _record_escalation(self, team_id: str, task_id: str, reason: str, *, version: int) -> None:
        """Judgment event: the decision unit must re-plan around this task."""
        if self._recorder is None:
            return
        self._recorder.record(
            team_id,
            type="wait.timeout",
            dedupe_key=f"task:{task_id}:escalation:{reason}:v{version}",
            actor="coordinator",
            payload={"task_id": task_id, "reason": reason},
        )

    def _refresh_status(self, team_id: str) -> None:
        runtime_repo = self._repos.team_runtime_repo
        runtime = runtime_repo.get(team_id)
        if runtime is None or runtime.status not in _RUNTIME_DISPATCH_STATUSES:
            return
        open_waits = self._repos.team_wait_repo.list_open_for_team(team_id)
        if open_waits:
            target = "waiting"
        elif self._repos.team_task_repo.count_open(team_id) > 0:
            target = "running"
        else:
            target = "idle"
        if runtime.status == target:
            return
        runtime_repo.update(
            team_id,
            expected_version=runtime.version,
            fields={"status": target},
        )


__all__ = [
    "DispatchOutcome",
    "DispatchStep",
    "DispatchTarget",
    "ResolveTarget",
    "SubmitPeer",
    "TeamDispatcher",
    "plan_dispatches",
    "task_sort_key",
]
