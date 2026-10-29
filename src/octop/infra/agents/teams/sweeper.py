"""Global team sweeper: due waits, timeouts, heartbeats and event replay.

Registered once per process as a cron system job (``team_sweeper``), it never
scans ``team_tasks`` to decide business work — dispatch is computed on demand
from the plan and wait ledger. It only handles time and crash recovery
(docs/team-drive-v2.md §5.4).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from octop.infra.agents.teams.decision import is_judgment_event
from octop.infra.agents.teams.task_state import is_valid_task_transition
from octop.infra.db.repos.team_waits import (
    WAIT_KIND_APPROVAL,
    WAIT_KIND_MEMBER_REPLY,
    WAIT_STATUS_CANCELLED,
    WAIT_STATUS_DEFERRED,
    WAIT_STATUS_SETTLED,
    WAIT_STATUS_TIMED_OUT,
)

logger = logging.getLogger(__name__)

SWEEPER_JOB_ID = "team_sweeper"
SWEEPER_INTERVAL_SEC = 60
DECISION_TIMEOUT_SEC = 600
EVENT_AGGREGATION_SEC = 5

_PAUSED_STATUSES = frozenset({"paused_user", "paused_budget"})
_TERMINAL_STATUSES = frozenset({"stopped", "finishing"})
_DECIDING_STATUSES = frozenset({"running", "waiting", "idle"})


class TeamSweeper:
    """One periodic pass over ``team_waits`` / ``team_decisions`` / runtime."""

    def __init__(
        self,
        *,
        repos: Any,
        coordinator: Any,
        timezone: str = "UTC",
        clock: Callable[[], float] = time.time,
        decision_timeout_sec: int = DECISION_TIMEOUT_SEC,
        aggregation_sec: int = EVENT_AGGREGATION_SEC,
    ) -> None:
        self._repos = repos
        self._coordinator = coordinator
        self._timezone = timezone
        self._clock = clock
        self._decision_timeout_sec = int(decision_timeout_sec)
        self._aggregation_sec = int(aggregation_sec)
        self._running = False

    def replace_repos(self, repos: Any) -> None:
        """Rebind after a control-plane database hot-swap."""
        self._repos = repos

    async def run_once(self) -> dict[str, int]:
        """Run one sweep; overlapping ticks are skipped, never queued."""
        if self._running:
            return {"skipped": 1}
        self._running = True
        try:
            now = int(self._clock())
            counts = {
                "due": self._sweep_due(now),
                "timeouts": self._sweep_timeouts(now),
                "decisions": self._sweep_decisions(now),
                "heartbeats": self._sweep_heartbeats(now),
                "events": await self._replay_events(now),
                "budget_reset": self._roll_budget(),
            }
            return counts
        finally:
            self._running = False

    # -- startup recovery -----------------------------------------------------

    def recover_inflight(self) -> int:
        """Fail dispatches whose in-process inbox job died with the last process.

        The harness inbox is process-local, so every ``member_reply`` wait
        still open at boot is stale: park the task and let a fresh decision
        re-plan (docs/team-drive-v2.md §11.1 step 2).
        """
        count = 0
        for wait in self._repos.team_wait_repo.list_open_by_kind(WAIT_KIND_MEMBER_REPLY):
            if not self._repos.team_wait_repo.settle(wait.wait_id, status=WAIT_STATUS_TIMED_OUT):
                continue
            self._notify_wait(wait.wait_id)
            self._park_task(wait)
            self._record(
                wait,
                type="wait.timeout",
                dedupe_key=f"wait:{wait.wait_id}:startup",
                payload={
                    "wait_id": wait.wait_id,
                    "kind": wait.kind,
                    "task_id": wait.task_id,
                    "reason": "process_restart",
                },
            )
            count += 1
        return count

    # -- waits ----------------------------------------------------------------

    def _sweep_due(self, now: int) -> int:
        count = 0
        for wait in self._repos.team_wait_repo.list_due(now=now):
            runtime = self._repos.team_runtime_repo.get(wait.team_id)
            status = getattr(runtime, "status", "")
            if status in _PAUSED_STATUSES:
                self._repos.team_wait_repo.settle(wait.wait_id, status=WAIT_STATUS_DEFERRED)
                self._notify_wait(wait.wait_id)
                continue
            if status in _TERMINAL_STATUSES:
                self._repos.team_wait_repo.settle(wait.wait_id, status=WAIT_STATUS_CANCELLED)
                self._notify_wait(wait.wait_id)
                continue
            if not self._repos.team_wait_repo.settle(wait.wait_id, status=WAIT_STATUS_SETTLED):
                continue
            self._notify_wait(wait.wait_id)
            self._clear_active_wait(wait.task_id, wait.wait_id)
            self._record(
                wait,
                type="wait.due",
                dedupe_key=f"wait:{wait.wait_id}:due",
                payload={
                    "wait_id": wait.wait_id,
                    "kind": wait.kind,
                    "task_id": wait.task_id,
                    "reason": (wait.payload or {}).get("reason"),
                },
            )
            count += 1
        return count

    def _sweep_timeouts(self, now: int) -> int:
        count = 0
        for wait in self._repos.team_wait_repo.list_timed_out(now=now):
            runtime = self._repos.team_runtime_repo.get(wait.team_id)
            status = getattr(runtime, "status", "")
            if status in _PAUSED_STATUSES:
                continue  # paused teams keep their waits untouched
            if status in _TERMINAL_STATUSES:
                self._repos.team_wait_repo.settle(wait.wait_id, status=WAIT_STATUS_CANCELLED)
                self._notify_wait(wait.wait_id)
                continue
            if not self._repos.team_wait_repo.settle(wait.wait_id, status=WAIT_STATUS_TIMED_OUT):
                continue
            if wait.kind == WAIT_KIND_APPROVAL:
                expire = getattr(self._coordinator, "expire_approval", None)
                if callable(expire):
                    expire(wait)
            self._notify_wait(wait.wait_id)
            self._park_task(wait)
            self._record(
                wait,
                type="wait.timeout",
                dedupe_key=f"wait:{wait.wait_id}:timeout",
                payload={
                    "wait_id": wait.wait_id,
                    "kind": wait.kind,
                    "task_id": wait.task_id,
                    "reason": "timeout",
                },
            )
            count += 1
        return count

    def _clear_active_wait(self, task_id: str | None, wait_id: str) -> None:
        if not task_id:
            return
        row = self._repos.team_task_repo.get(task_id)
        if row is None or row.active_wait_id != wait_id:
            return
        self._repos.team_task_repo.update(
            row.task_id,
            expected_version=row.version,
            fields={"active_wait_id": None},
        )

    def _park_task(self, wait: Any) -> None:
        """A timed-out wait leaves the task blocked for the next decision."""
        task_id = wait.task_id
        if not task_id:
            return
        row = self._repos.team_task_repo.get(task_id)
        if row is None:
            return
        fields: dict[str, object] = {"active_wait_id": None}
        if is_valid_task_transition(row.status, "blocked"):
            fields["status"] = "blocked"
            fields["blocked_reason"] = f"wait timed out: {wait.kind}"
        updated = self._repos.team_task_repo.update(
            task_id, expected_version=row.version, fields=fields
        )
        if updated is not None:
            self._coordinator.record_task_change(updated, actor="sweeper")

    # -- decisions / runtime --------------------------------------------------

    def _sweep_decisions(self, now: int) -> int:
        count = 0
        for decision in self._repos.team_decision_repo.list_stale(
            older_than=now - self._decision_timeout_sec
        ):
            self._repos.team_decision_repo.finish(
                decision.decision_id,
                status="failed",
                error="decision timed out in sweeper",
            )
            runtime = self._repos.team_runtime_repo.get(decision.team_id)
            if runtime is not None:
                fields: dict[str, object] = {}
                if runtime.status == "deciding":
                    fields["status"] = "running"
                if runtime.active_decision_id == decision.decision_id:
                    fields["active_decision_id"] = None
                if fields:
                    self._repos.team_runtime_repo.update(
                        decision.team_id,
                        expected_version=runtime.version,
                        fields=fields,
                    )
            self._coordinator.record(
                decision.team_id,
                type="decision.failed",
                dedupe_key=f"decision:{decision.decision_id}:timeout",
                actor="sweeper",
                payload={"decision_id": decision.decision_id, "reason": "timeout"},
            )
            count += 1
        return count

    def _sweep_heartbeats(self, now: int) -> int:
        count = 0
        for runtime in self._repos.team_runtime_repo.list_open():
            next_at = runtime.next_heartbeat_at
            if next_at is not None and next_at > now:
                continue
            interval = max(SWEEPER_INTERVAL_SEC, int(runtime.heartbeat_interval_sec or 1800))
            self._repos.team_runtime_repo.update(
                runtime.team_id,
                expected_version=runtime.version,
                fields={"next_heartbeat_at": now + interval},
            )
            self._coordinator.record_heartbeat(runtime.team_id, bucket=now // 60)
            if self._should_reconsider(runtime) and self._in_active_hours(runtime.team_id, now):
                self._coordinator.kick(runtime.team_id, reason="sweeper.heartbeat")
            count += 1
        return count

    def _in_active_hours(self, team_id: str, now: int) -> bool:
        """Whether this agent may be woken at ``now`` (policy duty window).

        The window lives on the autonomy policy; an agent without one (or with
        an empty window) is always eligible, which is the pre-policy default.
        """
        policy_repo = getattr(self._repos, "autonomy_policy_repo", None)
        if policy_repo is None:
            return True
        try:
            # Only an explicitly configured job spec constrains the schedule:
            # the defaults would otherwise put every team on a 09:00-22:00
            # window and silently stop its night-time wakes.
            policy = policy_repo.get_optional(team_id)
        except Exception:
            return True
        if policy is None:
            return True
        start = str(policy.active_hours_start or "").strip()
        end = str(policy.active_hours_end or "").strip()
        if not start or not end:
            return True
        try:
            clock = datetime.fromtimestamp(now, ZoneInfo(self._timezone)).strftime("%H:%M")
        except Exception:
            clock = datetime.now().strftime("%H:%M")
        return start <= clock < end

    def _should_reconsider(self, runtime: Any) -> bool:
        if runtime.status not in _DECIDING_STATUSES:
            return False
        if self._repos.team_decision_repo.get_active(runtime.team_id) is not None:
            return False
        if self._repos.team_wait_repo.list_open_for_team(runtime.team_id):
            return False
        return bool(self._repos.team_task_repo.count_open(runtime.team_id) > 0)

    async def _replay_events(self, now: int) -> int:
        """Crash recovery: schedule routing for events whose kick was lost.

        Never awaits a decision inline — the coordinator runs it in the
        background so one slow LLM call cannot stall due/timeout sweeps.
        """
        count = 0
        older_than = now - self._aggregation_sec
        for runtime in self._repos.team_runtime_repo.list_autopilot():
            events = self._repos.team_event_repo.list_unconsumed(
                runtime.team_id,
                older_than=older_than,
                limit=100,
            )
            if not events:
                continue
            need_decision = any(is_judgment_event(e.type, e.payload) for e in events)
            self._coordinator.kick(
                runtime.team_id,
                reason=events[0].type,
                need_decision=need_decision,
            )
            count += len(events)
        return count

    def _roll_budget(self) -> int:
        """Zero day counters at the server-timezone date boundary (§8.1)."""
        try:
            day = datetime.now(ZoneInfo(self._timezone)).strftime("%Y-%m-%d")
        except Exception:
            day = datetime.now().strftime("%Y-%m-%d")
        self._repos.team_runtime_repo.reset_budget(day=day, month=day[:7])
        return 1

    def _notify_wait(self, wait_id: str) -> None:
        notifier = getattr(self._coordinator, "notify_wait", None)
        if not callable(notifier):
            return
        row = self._repos.team_wait_repo.get(wait_id)
        if row is not None:
            notifier(row)

    def _record(
        self,
        wait: Any,
        *,
        type: str,
        dedupe_key: str,
        payload: dict[str, Any],
    ) -> None:
        self._coordinator.record(
            wait.team_id,
            type=type,
            dedupe_key=dedupe_key,
            actor="sweeper",
            payload=payload,
        )


def install_team_sweeper(
    cron_manager: Any,
    sweeper: TeamSweeper,
    *,
    interval_sec: int = SWEEPER_INTERVAL_SEC,
) -> None:
    cron_manager.schedule_system_job(
        SWEEPER_JOB_ID,
        trigger=f"interval:{int(interval_sec)}",
        func=sweeper.run_once,
    )


__all__ = [
    "DECISION_TIMEOUT_SEC",
    "EVENT_AGGREGATION_SEC",
    "SWEEPER_INTERVAL_SEC",
    "SWEEPER_JOB_ID",
    "TeamSweeper",
    "install_team_sweeper",
]
