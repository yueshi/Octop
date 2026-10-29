"""Team coordinator: persistent state machine for the drive loop.

Shadow mode (phase 1/2) appends events, triages routine vs judgment, and
optionally runs a record-only decision. Authoritative mode (phase 3) routes
events for teams whose ``team_runtime`` is ``mode=autopilot``: routine events
go straight to the dispatcher, judgment events run one decision and then
dispatch. Nothing here calls an execution adapter directly — dispatch is
delegated to ``TeamDispatcher``.

Instrumentation must never break the flow it observes, so entry points swallow
and log storage errors instead of propagating them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any

from octop.infra.agents.teams.decision import is_judgment_event
from octop.infra.agents.teams.task_state import is_valid_task_transition
from octop.infra.db.repos.team_waits import (
    WAIT_KIND_APPROVAL,
    WAIT_KIND_MEMBER_REPLY,
    WAIT_STATUS_CANCELLED,
    WAIT_STATUS_SETTLED,
    WAIT_STATUS_WAITING,
)

logger = logging.getLogger(__name__)

# Runtime states that still route control flow. ``idle`` is included so a
# freshly started team can plan its first goal; paused/finishing/stopped do
# not create new decisions or dispatches.
_ROUTE_STATUSES = frozenset({"idle", "running", "deciding", "dispatching", "waiting"})
_HARD_OFF_STATUSES = frozenset({"paused_user", "paused_budget", "stopped", "finishing"})
# Self-events from a decision's own plan application never trigger a decision.
_SELF_ACTOR_PREFIX = "host:"
_APPROVAL_WAIT_TTL_SEC = 1800


def _classify(event_type: str, payload: dict[str, Any] | None) -> str:
    """Rule-based routine/judgment triage (docs/team-drive-v2.md §5.3)."""
    return "judgment" if is_judgment_event(event_type, payload) else "routine"


class TeamCoordinator:
    """Observe events in shadow mode; own routing in authoritative mode.

    Instances are cheap stateless wrappers over repos; wiring sites that only
    need the ledger may build one on demand. Only the gateway-owned instance
    carries a dispatcher/decision runner and therefore routes work.
    """

    def __init__(
        self,
        *,
        event_repo: Any,
        runtime_repo: Any | None = None,
        task_repo: Any | None = None,
        wait_repo: Any | None = None,
        decision_runner: Any | None = None,
        dispatcher: Any | None = None,
        shadow_gate: Callable[[str], bool] | None = None,
        apply_plans: bool = False,
        frame_notify: Callable[[str, dict[str, Any]], None] | None = None,
        approval_expire: Callable[[str], None] | None = None,
        solo_gate: Callable[[str], bool] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._event_repo = event_repo
        self._runtime_repo = runtime_repo
        self._task_repo = task_repo
        self._wait_repo = wait_repo
        self._decision_runner = decision_runner
        self._dispatcher = dispatcher
        self._shadow_gate = shadow_gate
        self._apply_plans = apply_plans
        self._frame_notify = frame_notify
        self._approval_expire = approval_expire
        # A digital employee (single agent) is its own decision unit: there is
        # nobody to plan for, so running the LLM decision pass would only add
        # cost and latency between the wake and the work.
        self._solo_gate = solo_gate
        self._clock = clock
        # Strong refs keep fire-and-forget tasks alive mid-await.
        self._bg_tasks: set[asyncio.Task[None]] = set()
        # One routing pass per team at a time (single-process assumption §13).
        self._team_locks: dict[str, asyncio.Lock] = {}
        # Coalesce event storms: a scheduled pass already aggregates every
        # pending event, so later events just wait for the sweeper replay.
        self._scheduled_routing: set[str] = set()

    def bind_decision_runner(self, runner: Any | None) -> None:
        """Wire the runner after construction (it needs this coordinator as recorder)."""
        self._decision_runner = runner

    def bind_approval_expire(self, expire: Callable[[str], None] | None) -> None:
        """Wire ``HitlPendingStore.expire_for_task`` so wait timeouts kill the pause."""
        self._approval_expire = expire

    def expire_approval(self, wait: Any) -> None:
        """Expire a timed-out approval pause (zero-compute path, §7.2)."""
        if self._approval_expire is None:
            return
        task_id = wait.task_id or (wait.payload or {}).get("task_id")
        if not task_id:
            return
        with contextlib.suppress(Exception):
            self._approval_expire(str(task_id))

    def replace_repos(self, repos: Any) -> None:
        """Rebind after a control-plane database hot-swap."""
        self._event_repo = repos.team_event_repo
        self._runtime_repo = repos.team_runtime_repo
        self._task_repo = repos.team_task_repo
        self._wait_repo = repos.team_wait_repo
        runner = self._decision_runner
        replace = getattr(runner, "replace_repos", None)
        if callable(replace):
            replace(repos)

    # -- runtime state ---------------------------------------------------------

    def runtime(self, team_id: str) -> Any | None:
        if self._runtime_repo is None:
            return None
        return self._runtime_repo.get(team_id)

    def is_autopilot(self, team_id: str) -> bool:
        runtime = self.runtime(team_id)
        return bool(
            runtime is not None
            and runtime.mode == "autopilot"
            and runtime.status in _ROUTE_STATUSES
        )

    def _lock_for(self, team_id: str) -> asyncio.Lock:
        lock = self._team_locks.get(team_id)
        if lock is None:
            lock = asyncio.Lock()
            self._team_locks[team_id] = lock
        return lock

    # -- ingestion ------------------------------------------------------------

    def record(
        self,
        team_id: str | None,
        *,
        type: str,
        dedupe_key: str,
        actor: str = "",
        payload: dict[str, Any] | None = None,
        route: bool = True,
    ) -> str | None:
        """Append one event and kick routing; returns its ``event_id``.

        ``None`` means skipped: no team scope, a duplicate ``dedupe_key``
        (the fact is already on the ledger), or a storage failure — the
        ledger is instrumentation and must not break the flow.
        """
        if not team_id:
            return None
        try:
            if self._runtime_repo is not None:
                self._runtime_repo.ensure(team_id)
            row = self._event_repo.append(
                team_id,
                type=type,
                dedupe_key=dedupe_key,
                actor=actor,
                payload=payload,
            )
        except Exception:
            logger.warning("team event %s for %s not recorded", type, team_id, exc_info=True)
            return None
        if row is None:
            return None
        event_id = str(row.event_id)
        from octop.infra.metrics import METRICS  # noqa: PLC0415

        METRICS.inc("team_event_total")
        if _classify(type, payload) == "judgment":
            METRICS.inc("team_event_judgment_total")
        else:
            METRICS.inc("team_event_routine_total")
        if route:
            try:
                self._route_after_append(team_id, row)
            except Exception:
                logger.warning("team %s event routing failed", team_id, exc_info=True)
        return event_id

    def _route_after_append(self, team_id: str, event: Any) -> None:
        """Shadow decisions stay opt-in; autopilot events route for real."""
        judgment = _classify(event.type, event.payload) == "judgment"
        actor = str(getattr(event, "actor", "") or "")
        if actor.startswith(_SELF_ACTOR_PREFIX):
            # The decision's own board writes are audit only (§5.2).
            with contextlib.suppress(Exception):
                self._event_repo.mark_consumed(str(event.event_id))
            return
        if self._dispatcher is None or not self.is_autopilot(team_id):
            if judgment:
                self.maybe_run_shadow_decision(
                    team_id,
                    reason=event.type,
                    trigger_event_ids=[str(event.event_id)],
                )
            return
        self._schedule(
            team_id,
            [str(event.event_id)],
            need_decision=judgment,
            reason=event.type,
        )

    def kick(
        self,
        team_id: str,
        *,
        reason: str,
        need_decision: bool = True,
    ) -> asyncio.Task[None] | None:
        """Schedule a routing pass without a new event (start/resume/heartbeat)."""
        if self._dispatcher is None or not self.is_autopilot(team_id):
            return None
        return self._schedule(team_id, [], need_decision=need_decision, reason=reason)

    async def route_events(
        self,
        team_id: str,
        event_ids: list[str],
        *,
        need_decision: bool,
        reason: str,
    ) -> None:
        """Synchronous entry point for the sweeper's replay pass."""
        await self._run_authoritative(
            team_id,
            event_ids,
            need_decision=need_decision,
            reason=reason,
        )

    def _schedule(
        self,
        team_id: str,
        event_ids: list[str],
        *,
        need_decision: bool,
        reason: str,
    ) -> asyncio.Task[None] | None:
        # Kicks carry no event and must not be dropped: they are the only
        # signal for start/resume/heartbeat planning.
        if event_ids and team_id in self._scheduled_routing:
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        self._scheduled_routing.add(team_id)
        try:
            task = loop.create_task(
                self._run_authoritative(
                    team_id,
                    event_ids,
                    need_decision=need_decision,
                    reason=reason,
                )
            )
        except Exception:
            self._scheduled_routing.discard(team_id)
            logger.warning("team %s routing task not scheduled", team_id, exc_info=True)
            return None
        self._bg_tasks.add(task)
        task.add_done_callback(self._on_bg_done)
        task.add_done_callback(lambda _task: self._scheduled_routing.discard(team_id))
        return task

    # -- authoritative routing ------------------------------------------------

    async def _run_authoritative(
        self,
        team_id: str,
        event_ids: list[str],
        *,
        need_decision: bool,
        reason: str,
    ) -> None:
        async with self._lock_for(team_id):
            if not self.is_autopilot(team_id):
                return
            # Aggregate: one routing pass consumes every event pending at the
            # moment it starts, so an event storm yields at most one decision
            # (docs/team-drive-v2.md §5.2 / §11.3).
            pending = self._event_repo.list_unconsumed(team_id, limit=100)
            by_id = {str(event.event_id): event for event in pending}
            for event_id in event_ids:
                event = self._event_repo.get(event_id)
                if event is not None and event.consumed_at is None:
                    by_id[str(event.event_id)] = event
            fresh = list(by_id.values())
            if event_ids and not fresh:
                return  # another pass consumed this batch
            decision_id: str | None = None
            solo = bool(self._solo_gate(team_id)) if self._solo_gate is not None else False
            if not solo and (
                need_decision or any(_classify(e.type, e.payload) == "judgment" for e in fresh)
            ):
                runner = self._decision_runner
                if runner is None:
                    return
                runtime = self.runtime(team_id)
                prior = runtime.status if runtime is not None else "running"
                self._set_status(team_id, "deciding")
                try:
                    decision_id = await runner.run(
                        team_id,
                        trigger_reason=reason,
                        trigger_event_ids=[str(e.event_id) for e in fresh],
                        apply_plan=self._apply_plans,
                    )
                except Exception:
                    self._set_status(team_id, prior)
                    raise
                if decision_id is None:
                    # Cooldown / already-deciding: keep events for the next pass.
                    self._set_status(team_id, prior)
                    return
                self.notify_decision(team_id, decision_id)
            ids = [str(e.event_id) for e in fresh]
            if ids:
                with contextlib.suppress(Exception):
                    self._event_repo.mark_consumed_many(ids, decision_id=decision_id)
            if not self.is_autopilot(team_id):
                return
            if self._dispatcher is not None:
                self._set_status(team_id, "dispatching")
                try:
                    await self._dispatcher.dispatch_ready(team_id)
                except Exception:
                    logger.warning("team %s dispatch pass failed", team_id, exc_info=True)
                    self._set_status(team_id, "running")
                self.notify_runtime(team_id)

    def _set_status(self, team_id: str, target: str) -> None:
        repo = self._runtime_repo
        if repo is None:
            return
        try:
            row = repo.get(team_id) or repo.ensure(team_id)
        except Exception:
            return
        if row.status in _HARD_OFF_STATUSES or row.status == target:
            return
        with contextlib.suppress(Exception):
            repo.update(team_id, expected_version=row.version, fields={"status": target})

    # -- typed helpers, one per event source -----------------------------------

    def record_member_reply(
        self,
        *,
        team_id: str | None,
        member_id: str,
        status: str,
        inbox_id: str,
        excerpt: str = "",
    ) -> str | None:
        return self.record(
            team_id,
            type="member.replied",
            dedupe_key=f"inbox:{inbox_id}",
            actor=f"member:{member_id}" if member_id else "member",
            payload={"status": status, "member_id": member_id, "excerpt": excerpt[:200]},
        )

    def handle_member_reply(self, event: Any) -> None:
        """Settle the dispatch wait and close the task before routing runs.

        Synchronous on purpose: ``record`` already scheduled the routing pass,
        and the background task must see the settled board when it starts.
        """
        wait_repo = self._wait_repo
        task_repo = self._task_repo
        if wait_repo is None or task_repo is None:
            return
        inbox_id = str(getattr(event, "inbox_id", "") or "")
        wait = wait_repo.get_open_by_external_ref(inbox_id, kind=WAIT_KIND_MEMBER_REPLY)
        if wait is None:
            # The sweeper may have timed the dispatch out; a late reply still
            # owns the task and must close it.
            wait = wait_repo.get_by_external_ref(inbox_id, kind=WAIT_KIND_MEMBER_REPLY)
        if wait is None:
            return
        status = str(getattr(event, "status", "") or "")
        if wait.status == WAIT_STATUS_WAITING:
            settle_status = WAIT_STATUS_CANCELLED if status == "cancelled" else WAIT_STATUS_SETTLED
            with contextlib.suppress(Exception):
                wait_repo.settle(wait.wait_id, status=settle_status)
            settled = wait_repo.get(wait.wait_id)
            if settled is not None:
                self.notify_wait(settled)
        task = task_repo.get(wait.task_id) if wait.task_id else None
        if task is None:
            return
        target = {"done": "done", "failed": "failed", "cancelled": "cancelled"}.get(status)
        if target is None or not is_valid_task_transition(task.status, target):
            return
        excerpt = str(getattr(event, "reply_text", "") or getattr(event, "error_text", "") or "")
        with contextlib.suppress(Exception):
            updated = task_repo.update(
                task.task_id,
                expected_version=task.version,
                fields={
                    "status": target,
                    "result_summary": excerpt[:1000],
                    "active_wait_id": None,
                },
            )
            if updated is not None:
                member_id = str(getattr(event, "target_agent_id", "") or "")
                self.record_task_change(updated, actor=f"member:{member_id}")

    def record_task_change(self, row: Any, *, actor: str, emit: bool = True) -> str | None:
        event_id = self.record(
            row.team_id,
            type="task.changed",
            dedupe_key=f"task:{row.task_id}:v{row.version}",
            actor=actor,
            payload={"task_id": row.task_id, "status": row.status, "title": row.title},
        )
        if event_id is not None and emit:
            self._emit(
                row.team_id,
                {
                    "type": "team.task.updated",
                    "team_id": row.team_id,
                    "task_id": row.task_id,
                    "status": row.status,
                    "version": row.version,
                },
            )
        return event_id

    def record_goal_change(self, row: Any, *, actor: str, emit: bool = True) -> str | None:
        event_id = self.record(
            row.team_id,
            type="goal.changed",
            # ``team_goals`` has no version column and ``updated_at`` is
            # second-resolution, so two edits inside one second collided on
            # the UNIQUE dedupe key and the second event was dropped — the
            # board then showed stale state and the coordinator never
            # re-planned. Goals are edited by a human, so there is no retry
            # path to deduplicate; a monotonic nonce is the right key.
            dedupe_key=f"goal:{row.goal_id}:{row.updated_at}:{time.time_ns()}",
            actor=actor,
            payload={"goal_id": row.goal_id, "status": row.status, "title": row.title},
        )
        if event_id is not None and emit:
            self._emit(
                row.team_id,
                {
                    "type": "team.goal.updated",
                    "team_id": row.team_id,
                    "goal_id": row.goal_id,
                    "status": row.status,
                },
            )
        return event_id

    def notify_wait(self, wait: Any) -> None:
        """Push a ``team.wait.updated`` frame (dispatcher/sweeper call sites)."""
        self._emit(
            wait.team_id,
            {
                "type": "team.wait.updated",
                "team_id": wait.team_id,
                "wait_id": wait.wait_id,
                "kind": wait.kind,
                "status": wait.status,
                "task_id": wait.task_id,
            },
        )

    def notify_decision(self, team_id: str, decision_id: str) -> None:
        self._emit(
            team_id,
            {"type": "team.decision.updated", "team_id": team_id, "decision_id": decision_id},
        )

    def notify_runtime(self, team_id: str) -> None:
        runtime = self.runtime(team_id)
        if runtime is None:
            return
        self._emit(
            team_id,
            {
                "type": "team.runtime.updated",
                "team_id": team_id,
                "status": runtime.status,
                "mode": runtime.mode,
                "active_decision_id": runtime.active_decision_id,
            },
        )

    def _emit(self, team_id: str, frame: dict[str, Any]) -> None:
        if self._frame_notify is None:
            return
        with contextlib.suppress(Exception):
            self._frame_notify(team_id, frame)

    def task_for_inbox(self, job_id: str) -> str | None:
        """Board task behind an in-flight inbox dispatch (member HITL linking)."""
        if self._wait_repo is None or not job_id:
            return None
        wait = self._wait_repo.get_open_by_external_ref(str(job_id), kind=WAIT_KIND_MEMBER_REPLY)
        return wait.task_id if wait is not None else None

    def record_approval_pending(self, record: Any) -> None:
        """Open an ``approval`` wait when a team member pauses for HITL.

        ``hitl_pending`` stays the runtime/UI store; the wait links to it via
        ``external_ref`` and carries the board task so a rejection can route a
        judgment.
        """
        wait_repo = self._wait_repo
        task_repo = self._task_repo
        task_id = getattr(record, "task_id", None)
        if wait_repo is None or task_repo is None or not task_id:
            return
        task = task_repo.get(str(task_id))
        if task is None:
            return
        pending_id = str(getattr(record, "pending_id", "") or "")
        now = int(self._clock())
        expires = now + _APPROVAL_WAIT_TTL_SEC
        with contextlib.suppress(Exception):
            wait = wait_repo.get_by_idempotency_key(f"approval:{pending_id}")
            if wait is None:
                wait = wait_repo.create(
                    task.team_id,
                    kind=WAIT_KIND_APPROVAL,
                    idempotency_key=f"approval:{pending_id}",
                    task_id=task.task_id,
                    timeout_at=expires,
                    deadline_at=task.due_at,
                    external_ref=pending_id,
                    payload={
                        "pending_id": pending_id,
                        "agent_id": str(getattr(record, "agent_id", "") or ""),
                        "thread_id": str(getattr(record, "thread_id", "") or ""),
                    },
                )
            if wait is None:
                return
            self.notify_wait(wait)
            if task.active_wait_id != wait.wait_id:
                task_repo.update(
                    task.task_id,
                    expected_version=task.version,
                    fields={"active_wait_id": wait.wait_id},
                )
            current = task_repo.get(task.task_id)
            if current is not None and is_valid_task_transition(current.status, "needs_input"):
                updated = task_repo.update(
                    current.task_id,
                    expected_version=current.version,
                    fields={
                        "status": "needs_input",
                        "blocked_reason": "waiting for creator approval",
                    },
                )
                if updated is not None:
                    self.record_task_change(updated, actor="coordinator")
            # A member run may keep working after the approval; do not let the
            # dispatch wait time out while the creator thinks.
            for open_wait in wait_repo.list_open_for_task(
                task.task_id, kind=WAIT_KIND_MEMBER_REPLY
            ):
                wait_repo.set_timeout(open_wait.wait_id, expires + 300)

    def record_hitl_resolved(
        self,
        *,
        task_id: str | None,
        pending_id: str,
        status: str,
        agent_id: str = "",
    ) -> str | None:
        """Team-scoped only: non-team pauses have no task to resolve through."""
        if task_id is None or self._task_repo is None:
            return None
        task = self._task_repo.get(task_id)
        if task is None:
            return None
        event_id = self.record(
            task.team_id,
            type="approval.resolved",
            dedupe_key=f"hitl:{pending_id}:{status}",
            actor="user",
            payload={
                "pending_id": pending_id,
                "status": status,
                "task_id": task_id,
                "agent_id": agent_id,
            },
        )
        self._settle_approval(task_id, status)
        return event_id

    def resolve_approval_wait(
        self,
        team_id: str,
        wait_id: str,
        *,
        status: str,
        actor: str,
        note: str = "",
    ) -> tuple[Any, Any | None]:
        """Approve/reject one decision-level ``approval`` wait.

        Only waits without an ``external_ref`` are handled here: a ref means a
        member HITL pause, which must be resolved through its conversation card
        so the member graph can resume (docs/team-drive-v2.md §7.4).
        """
        if status not in ("approved", "rejected"):
            raise ValueError("status must be approved or rejected")
        wait_repo = self._wait_repo
        task_repo = self._task_repo
        if wait_repo is None:
            raise ValueError("approval ledger is not available")
        wait = wait_repo.get(wait_id)
        if wait is None or wait.team_id != team_id:
            raise ValueError("approval wait not found")
        if wait.kind != WAIT_KIND_APPROVAL:
            raise ValueError("only approval waits can be resolved")
        if wait.external_ref:
            raise ValueError("member HITL pauses are resolved in the conversation")
        if wait.status != WAIT_STATUS_WAITING:
            raise ValueError("approval wait is no longer open")

        updated: Any | None = None
        if task_repo is not None and wait.task_id:
            task = task_repo.get(wait.task_id)
            if task is not None:
                if task.active_wait_id not in (None, wait_id):
                    raise ValueError("task is waiting on a different approval")
                fields: dict[str, object] = {}
                if task.active_wait_id == wait_id:
                    fields["active_wait_id"] = None
                terminal = task.status in ("done", "failed", "cancelled")
                if status == "approved":
                    # A plan-level approval normally gates a task that was
                    # never dispatched; make it ready again. If a member run
                    # is still in flight, resume it as running.
                    in_flight = bool(
                        wait_repo.list_open_for_task(task.task_id, kind=WAIT_KIND_MEMBER_REPLY)
                    )
                    target = "running" if in_flight else "proposed"
                    if not in_flight and not is_valid_task_transition(task.status, target):
                        target = "running"
                else:
                    target = "blocked"
                if not terminal and is_valid_task_transition(task.status, target):
                    fields["status"] = target
                if not terminal:
                    fields["blocked_reason"] = (
                        (note or "approval rejected").strip() if status == "rejected" else ""
                    )
                if fields:
                    updated = task_repo.update(
                        wait.task_id,
                        expected_version=task.version,
                        fields=fields,
                    )
                    if updated is None:
                        raise ValueError("task changed concurrently; retry")

        settle_status = WAIT_STATUS_SETTLED if status == "approved" else WAIT_STATUS_CANCELLED
        if not wait_repo.settle(wait_id, status=settle_status):
            raise ValueError("approval wait is no longer open")
        fresh = wait_repo.get(wait_id)
        if fresh is not None:
            self.notify_wait(fresh)
        self.record(
            team_id,
            type="approval.resolved",
            dedupe_key=f"wait:{wait_id}:resolved",
            actor=actor,
            payload={
                "wait_id": wait_id,
                "task_id": wait.task_id,
                "status": status,
                "note": note.strip(),
            },
        )
        if updated is not None:
            self.record_task_change(updated, actor=actor)
        return fresh or wait, updated

    def _settle_approval(self, task_id: str, status: str) -> None:
        wait_repo = self._wait_repo
        task_repo = self._task_repo
        if wait_repo is None or task_repo is None:
            return
        approved = status == "approved"
        with contextlib.suppress(Exception):
            for wait in wait_repo.list_open_for_task(task_id, kind=WAIT_KIND_APPROVAL):
                wait_repo.settle(
                    wait.wait_id,
                    status=WAIT_STATUS_SETTLED if approved else WAIT_STATUS_CANCELLED,
                )
                settled = wait_repo.get(wait.wait_id)
                if settled is not None:
                    self.notify_wait(settled)
            row = task_repo.get(task_id)
            if row is None:
                return
            target = None
            if approved and row.status == "needs_input":
                target = "running"
            elif not approved and row.status in ("needs_input", "assigned", "running"):
                target = "blocked"
            fields: dict[str, object] = {"active_wait_id": None}
            if target is not None:
                fields["status"] = target
                if not approved:
                    fields["blocked_reason"] = f"approval {status}"
            updated = task_repo.update(task_id, expected_version=row.version, fields=fields)
            if updated is not None:
                self.record_task_change(updated, actor="user")

    def record_user_message(
        self,
        *,
        team_id: str | None,
        session_key: str = "",
        channel_type: str = "",
        excerpt: str = "",
    ) -> str | None:
        # No stable message id exists at ingress, so the wall-clock nonce
        # keeps the key unique without pretending idempotency we lack.
        return self.record(
            team_id,
            type="user.message",
            dedupe_key=f"usermsg:{session_key}:{time.time_ns()}",
            actor="user",
            payload={
                "session": session_key,
                "channel_type": channel_type,
                "excerpt": excerpt[:200],
            },
        )

    def record_boot(self, team_id: str) -> str | None:
        return self.record(
            team_id,
            type="runtime.boot",
            dedupe_key=f"boot:{team_id}:{time.time_ns()}",
            actor="system",
        )

    def record_heartbeat(self, team_id: str, *, bucket: int) -> str | None:
        return self.record(
            team_id,
            type="sweeper.heartbeat",
            dedupe_key=f"hb:{team_id}:{bucket}",
            actor="sweeper",
        )

    # -- shadow decision trigger (phase 2, §6) ---------------------------------

    def maybe_run_shadow_decision(
        self,
        team_id: str,
        *,
        reason: str,
        trigger_event_ids: list[str] | None = None,
    ) -> asyncio.Task[None] | None:
        """Fire the opt-in shadow decision in the background.

        Returns the spawned task (kept referenced so it is not
        garbage-collected mid-await), or ``None`` when unwired, gated off,
        or called outside a running loop — sync contexts get their decision
        via the phase-3 sweeper instead.
        """
        runner = self._decision_runner
        gate = self._shadow_gate
        if runner is None or gate is None:
            return None
        with contextlib.suppress(Exception):
            if not gate(team_id):
                return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        task = loop.create_task(
            runner.run(team_id, trigger_reason=reason, trigger_event_ids=trigger_event_ids)
        )
        self._bg_tasks.add(task)
        task.add_done_callback(self._on_bg_done)
        return task

    def _on_bg_done(self, task: asyncio.Task[None]) -> None:
        self._bg_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("team routing task failed", exc_info=task.exception())
