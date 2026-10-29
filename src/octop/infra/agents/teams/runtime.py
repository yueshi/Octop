"""Team autopilot runtime: start/pause/resume/finish/stop and legacy migration.

``team_runtime`` is the authoritative state machine (docs/team-drive-v2.md §7.7).
This module owns the transitions and the small amount of wiring needed to
execute them: resolve the team's current room, submit peers through the
harness inbox, notify the creator, and migrate legacy ``team_drive`` cron rows.

It deliberately does not import the coordinator: the gateway builds both and
calls :meth:`TeamAutopilot.bind_coordinator` so ``start``/``resume`` can kick
the first decision.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from octop.infra.agents.teams.dispatcher import DispatchTarget
from octop.infra.agents.teams.drive import (
    TEAM_DRIVE_INTERVAL_FLOOR_SEC,
    is_team_drive_job,
    stop_team_drive,
    validate_drive_trigger,
)
from octop.infra.db.repos.team_waits import WAIT_KIND_MEMBER_REPLY, WAIT_KIND_TIMER
from octop.infra.gateway.threads import ThreadRegistry

logger = logging.getLogger(__name__)

DEFAULT_HEARTBEAT_SEC = 1800
HEARTBEAT_FLOOR_SEC = TEAM_DRIVE_INTERVAL_FLOOR_SEC
_AUTOPILOT_ACTIVE_STATUSES = frozenset({"running", "deciding", "dispatching", "waiting"})

NotifyTeam = Callable[[str, str], Awaitable[None]]
"""Async room notification: (team_id, text)."""


def _parse_trigger(trigger: str) -> tuple[str, int | None]:
    """Return (kind, seconds-or-epoch) for an interval/date drive trigger."""
    value = str(trigger or "")
    kind, _, rest = value.partition(":")
    if kind == "interval":
        with contextlib.suppress(ValueError):
            return "interval", int(rest)
        return "interval", None
    if kind == "date":
        with contextlib.suppress(ValueError):
            parsed = datetime.fromisoformat(rest)
            if parsed.tzinfo is None:
                parsed = parsed.astimezone()
            return "date", int(parsed.timestamp())
    return kind or "", None


class TeamAutopilot:
    """Runtime control plane for one team's autopilot loop."""

    def __init__(
        self,
        *,
        repos: Any,
        agent_manager: Any,
        thread_registry: ThreadRegistry,
        notify: NotifyTeam | None = None,
        frame_notify: Callable[[str, dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._repos = repos
        self._agent_manager = agent_manager
        self._threads = thread_registry
        self._notify = notify
        self._frame_notify = frame_notify
        self._clock = clock
        self._coordinator: Any | None = None
        # Strong refs keep fire-and-forget self turns alive mid-await.
        self._self_tasks: set[asyncio.Task[None]] = set()

    def bind_coordinator(self, coordinator: Any | None) -> None:
        self._coordinator = coordinator

    def replace_repos(self, repos: Any) -> None:
        """Rebind after a control-plane database hot-swap."""
        self._repos = repos

    # -- reads ----------------------------------------------------------------

    def runtime(self, team_id: str) -> Any | None:
        return self._repos.team_runtime_repo.get(team_id)

    def is_autopilot(self, team_id: str) -> bool:
        row = self.runtime(team_id)
        return bool(
            row is not None and row.mode == "autopilot" and row.status in _AUTOPILOT_ACTIVE_STATUSES
        )

    def status_payload(self, team_id: str) -> dict[str, Any]:
        runtime = self._repos.team_runtime_repo.ensure(team_id)
        goal = self._repos.team_goal_repo.active(team_id)
        decision = self._repos.team_decision_repo.get_active(team_id)
        waits = self._repos.team_wait_repo.list_open_for_team(team_id)
        return {
            "team_id": runtime.team_id,
            "status": runtime.status,
            "mode": runtime.mode,
            "goal_id": runtime.goal_id or (goal.goal_id if goal is not None else None),
            "active_decision_id": decision.decision_id if decision is not None else None,
            "last_decision_at": runtime.last_decision_at,
            "next_heartbeat_at": runtime.next_heartbeat_at,
            "heartbeat_interval_sec": runtime.heartbeat_interval_sec,
            "budget_day": runtime.budget_day,
            "budget_used": runtime.budget_used,
            "budget_reserved": runtime.budget_reserved,
            "paused_reason": runtime.paused_reason,
            "version": runtime.version,
            "open_waits": [
                {
                    "wait_id": wait.wait_id,
                    "kind": wait.kind,
                    "status": wait.status,
                    "task_id": wait.task_id,
                    "due_at": wait.due_at,
                    "timeout_at": wait.timeout_at,
                    "external_ref": wait.external_ref,
                    "reason": str((wait.payload or {}).get("reason") or ""),
                }
                for wait in waits
            ],
        }

    # -- transitions ----------------------------------------------------------

    async def start(
        self,
        team_id: str,
        *,
        user_id: int | None = None,
        heartbeat_interval_sec: int | None = None,
        trigger: str | None = None,
    ) -> Any:
        """Start (or resume) the autopilot loop; idempotent for running teams."""
        now = int(self._clock())
        if trigger:
            validate_drive_trigger(trigger)
        interval = self._resolve_interval(heartbeat_interval_sec, trigger)
        self._ensure(team_id)
        if trigger:
            await self._one_shot_wait(team_id, trigger, now)
        self._update(
            team_id,
            {
                "status": "running",
                "mode": "autopilot",
                "paused_reason": None,
                "heartbeat_interval_sec": interval,
                "next_heartbeat_at": now + interval,
                # An explicit user start bypasses the post-decision cooldown.
                "last_decision_at": None,
            },
        )
        await self._drop_legacy_drive_jobs(team_id)
        self._record(
            team_id,
            type="runtime.boot",
            dedupe_key=f"runtime:start:{team_id}:{time.time_ns()}",
            actor=f"user:{user_id}" if user_id is not None else "coordinator",
            route=False,
        )
        if self._has_work(team_id):
            self._kick(team_id, reason="drive.start")
        self._push_runtime(team_id)
        return self.runtime(team_id)

    async def pause(self, team_id: str, *, reason: str | None = None) -> Any:
        self._ensure(team_id)
        self._update(
            team_id,
            {"status": "paused_user", "paused_reason": reason or "paused_by_user"},
        )
        self._record(
            team_id,
            type="runtime.paused",
            dedupe_key=f"runtime:pause:{team_id}:{time.time_ns()}",
            actor="user",
            payload={"reason": reason or "paused_by_user"},
            route=False,
        )
        self._push_runtime(team_id)
        return self.runtime(team_id)

    async def resume(self, team_id: str) -> Any:
        now = int(self._clock())
        runtime = self._ensure(team_id)
        self._repos.team_wait_repo.reactivate_deferred(team_id)
        self._update(
            team_id,
            {
                "status": "running",
                "mode": "autopilot",
                "paused_reason": None,
                "next_heartbeat_at": now + int(runtime.heartbeat_interval_sec),
            },
        )
        self._record(
            team_id,
            type="runtime.resumed",
            dedupe_key=f"runtime:resume:{team_id}:{time.time_ns()}",
            actor="user",
            route=False,
        )
        if self._has_work(team_id):
            self._kick(team_id, reason="drive.resume")
        self._push_runtime(team_id)
        return self.runtime(team_id)

    async def finish(self, team_id: str) -> Any:
        self._ensure(team_id)
        goal = self._repos.team_goal_repo.active(team_id)
        if goal is not None:
            updated = self._repos.team_goal_repo.update(goal.goal_id, fields={"status": "done"})
            if updated is not None and self._coordinator is not None:
                self._coordinator.record_goal_change(updated, actor="user")
        self._update(team_id, {"status": "finishing", "paused_reason": "goal_finished"})
        self._record(
            team_id,
            type="runtime.finished",
            dedupe_key=f"runtime:finish:{team_id}:{time.time_ns()}",
            actor="user",
            route=False,
        )
        self._push_runtime(team_id)
        return self.runtime(team_id)

    async def stop(
        self,
        team_id: str,
        *,
        cron_manager: Any | None = None,
        team_inbox: Any | None = None,
    ) -> dict[str, int]:
        """Kill switch: cancel waits, open tasks, legacy cron rows, inbox work."""
        cron_manager = cron_manager or getattr(self._agent_manager, "cron_manager", None)
        if team_inbox is None:
            getter = getattr(self._agent_manager, "team_inbox", None)
            team_inbox = getter() if callable(getter) else None
        counts = await stop_team_drive(
            team_id=team_id,
            cron_manager=cron_manager,
            task_repo=self._repos.team_task_repo,
            team_inbox=team_inbox,
        )
        counts["cancelled_waits"] = self._repos.team_wait_repo.cancel_open_for_team(team_id)
        self._ensure(team_id)
        self._update(team_id, {"status": "stopped", "paused_reason": "stopped_by_user"})
        self._record(
            team_id,
            type="runtime.stopped",
            dedupe_key=f"runtime:stop:{team_id}:{time.time_ns()}",
            actor="user",
            payload={key: int(value) for key, value in counts.items()},
            route=False,
        )
        self._push_runtime(team_id)
        return counts

    # -- legacy migration -----------------------------------------------------

    async def migrate_legacy_drive_jobs(self, cron_manager: Any) -> int:
        """Fold ``cron_jobs.meta.kind=team_drive`` rows into ``team_runtime``."""
        migrated = 0
        now = int(self._clock())
        for row in self._repos.cron_repo.list_all(include_disabled=True):
            if not is_team_drive_job(row):
                continue
            team_id = row.agent_id
            try:
                moved = await self._migrate_one(row, team_id, now)
            except Exception:
                logger.warning("legacy drive job %s not migrated", row.cron_id, exc_info=True)
                continue
            if not moved:
                continue
            with contextlib.suppress(Exception):
                await cron_manager.delete(row.cron_id)
                migrated += 1
        if migrated:
            logger.info("migrated %d legacy team drive cron job(s) into team_runtime", migrated)
        return migrated

    async def _migrate_one(self, row: Any, team_id: str, now: int) -> bool:
        runtime = self._ensure(team_id)
        if row.enabled:
            kind, value = _parse_trigger(str(getattr(row, "trigger", "") or ""))
            interval = max(
                HEARTBEAT_FLOOR_SEC,
                value if kind == "interval" and value else int(runtime.heartbeat_interval_sec),
            )
            if runtime.status not in ("paused_user", "paused_budget", "stopped", "finishing"):
                self._update(
                    team_id,
                    {
                        "status": "running",
                        "mode": "autopilot",
                        "paused_reason": None,
                        "heartbeat_interval_sec": interval,
                        "next_heartbeat_at": now + interval,
                    },
                )
            if kind == "date" and value:
                await self._one_shot_wait(team_id, str(row.trigger), now)
        self._migrate_inflight_tasks(team_id)
        self._record(
            team_id,
            type="runtime.boot",
            dedupe_key=f"migrate:{row.cron_id}",
            actor="system",
            payload={"cron_id": row.cron_id},
            route=False,
        )
        return True

    def _migrate_inflight_tasks(self, team_id: str) -> int:
        """Park legacy in-flight tasks whose inbox state died with v1.

        A legacy drive dispatch has no ``team_waits`` row, so the coordinator
        cannot settle or retry it; mark it blocked for the first decision
        (docs/team-drive-v2.md §11.1 step 2) instead of leaving it busy forever.
        """
        moved = 0
        for status in ("assigned", "running"):
            for task in self._repos.team_task_repo.list_by_team(team_id, status=status):
                if self._repos.team_wait_repo.list_open_for_task(
                    task.task_id, kind=WAIT_KIND_MEMBER_REPLY
                ):
                    continue
                updated = self._repos.team_task_repo.update(
                    task.task_id,
                    expected_version=task.version,
                    fields={
                        "status": "blocked",
                        "blocked_reason": "migrated: in-flight dispatch state unknown",
                        "active_wait_id": None,
                    },
                )
                if updated is None:
                    continue
                moved += 1
                self._record(
                    team_id,
                    type="task.changed",
                    dedupe_key=f"task:{updated.task_id}:v{updated.version}",
                    actor="system",
                    payload={
                        "task_id": updated.task_id,
                        "status": updated.status,
                        "title": updated.title,
                    },
                    route=False,
                )
        return moved

    # -- execution adapter ----------------------------------------------------

    async def resolve_target(self, team_id: str) -> DispatchTarget | None:
        """The team's most recent room, falling back to its dashboard session."""
        row = self._repos.agent_repo.get(team_id)
        if row is None or row.user_id is None:
            return None
        user_id = int(row.user_id)
        threads = self._threads.list_threads(agent_id=team_id, user_id=user_id, limit=1)
        if threads:
            thread = threads[0]
            return DispatchTarget(
                team_id=team_id,
                user_id=user_id,
                thread_id=str(thread.thread_id),
                session_key=str(thread.session_key),
            )
        session_key = ThreadRegistry.dashboard_key(agent_id=team_id, user_id=user_id)
        thread_id = await self._threads.get_or_create_by_key(
            session_key=session_key,
            agent_id=team_id,
            user_id=user_id,
            channel_type=ThreadRegistry.CHANNEL_DASHBOARD,
            channel_chat_type=ThreadRegistry.CHAT_TYPE_DM,
        )
        return DispatchTarget(
            team_id=team_id,
            user_id=user_id,
            thread_id=str(thread_id),
            session_key=session_key,
        )

    async def submit_peer(
        self,
        team_id: str,
        member_id: str,
        message: str,
        target: DispatchTarget,
    ) -> str | None:
        if member_id == team_id:
            # Self-execute: harness forbids a peer call to oneself
            # ("cannot collaborate with self"), and a digital employee has
            # nobody to dispatch to — it runs the turn itself.
            return self._submit_self(team_id, message, target)
        harness = getattr(self._agent_manager, "harness_manager", None)
        team = getattr(harness, "team", None)
        if team is None:
            raise RuntimeError("harness team manager is not ready")
        result = team.submit_peer(
            from_agent_id=team_id,
            to_agent_id=member_id,
            message=message,
            user_id=target.user_id,
            source_thread_id=target.thread_id,
            metadata={"session_key": target.session_key},
        )
        return str(result.job_id) if getattr(result, "job_id", None) else None

    def _submit_self(self, team_id: str, message: str, target: DispatchTarget) -> str:
        """Queue one self-executed turn; returns the synthetic job id.

        The id becomes the dispatch wait's ``external_ref``, so the reply
        settles the same wait a member call would.
        """
        job_id = f"self:{team_id}:{uuid.uuid4().hex[:10]}"
        task = asyncio.create_task(self._run_self_turn(team_id, job_id, message, target))
        self._self_tasks.add(task)
        task.add_done_callback(self._self_tasks.discard)
        return job_id

    async def _run_self_turn(
        self,
        team_id: str,
        job_id: str,
        message: str,
        target: DispatchTarget,
    ) -> None:
        """Run one turn on the agent itself, then report it like a member reply.

        Uses the same entry point as a scheduled turn, so metering, HITL and
        history all behave exactly as they do for any other attended turn.
        """
        from octop.infra.gateway.process.harness_request import (  # noqa: PLC0415
            build_harness_request,
        )

        parts: list[str] = []
        status = "done"
        error_text = ""
        paused = False
        usage_tracker: Any | None = None
        try:
            from octop.infra.gateway.process.usage_record import UsageTracker  # noqa: PLC0415

            usage_tracker = UsageTracker()
            request = build_harness_request(
                thread_id=target.thread_id,
                user_id=target.user_id,
                agent_id=team_id,
                session_key=target.session_key,
                source=ThreadRegistry.CHANNEL_DASHBOARD,
                text=message,
            )
            async for chunk in self._agent_manager.stream(team_id, request):
                kind = chunk.get("type")
                if usage_tracker is not None:
                    usage_tracker.observe(chunk)
                if kind in ("token", "delta"):
                    parts.append(str(chunk.get("content") or chunk.get("text") or ""))
                elif kind == "hitl_required":
                    paused = True
        except Exception as exc:
            logger.warning("team %s self-execute failed", team_id, exc_info=True)
            status = "failed"
            error_text = f"{type(exc).__name__}: {exc}"

        # Meter the turn. A self-executed turn never passes through
        # ``GlobalProcessor``, so without this the spend is invisible and the
        # budget gate reads zero forever (S2.6).
        self._record_self_usage(team_id, target, usage_tracker)

        coordinator = self._coordinator
        if coordinator is None:
            return
        if paused:
            # The pause owns the task now: the approval wait (registered by
            # the HITL path) settles it, so leave the dispatch wait open.
            return
        reply = "".join(parts).strip()
        coordinator.record_member_reply(
            team_id=team_id,
            member_id=team_id,
            status=status,
            inbox_id=job_id,
            excerpt=(reply or error_text)[:200],
        )
        coordinator.handle_member_reply(
            SimpleNamespace(
                inbox_id=job_id,
                status=status,
                target_agent_id=team_id,
                reply_text=reply,
                error_text=error_text,
            )
        )
        if reply:
            await self.notify(team_id, reply)

    def _record_self_usage(self, team_id: str, target: DispatchTarget, tracker: Any | None) -> None:
        usage_repo = getattr(self._repos, "usage_repo", None)
        runtime_repo = getattr(self._repos, "team_runtime_repo", None)
        if tracker is None or usage_repo is None:
            return
        usage = getattr(tracker, "usage", None)
        if not usage:
            return
        from octop.infra.gateway.process.usage_record import record_turn_usage  # noqa: PLC0415

        # ``team_id`` is the budget owner: a digital employee is metered under
        # its own runtime row, so ``budget_remaining`` sees the spend.
        record_turn_usage(
            usage_repo,
            agent_id=team_id,
            user_id=int(target.user_id),
            thread_id=target.thread_id,
            usage=usage,
            source="self_execute",
            team_id=team_id,
            runtime_repo=runtime_repo,
        )

    async def notify(self, team_id: str, text: str) -> None:
        if self._notify is not None and text.strip():
            await self._notify(team_id, text.strip())

    # -- internals ------------------------------------------------------------

    def _ensure(self, team_id: str) -> Any:
        return self._repos.team_runtime_repo.ensure(team_id)

    def _update(self, team_id: str, fields: dict[str, object]) -> Any:
        """CAS update with one retry against a concurrent coordinator write."""
        for _ in range(2):
            row = self._ensure(team_id)
            updated = self._repos.team_runtime_repo.update(
                team_id,
                expected_version=row.version,
                fields=fields,
            )
            if updated is not None:
                return updated
        return self.runtime(team_id)

    def _record(
        self,
        team_id: str,
        *,
        type: str,
        dedupe_key: str,
        actor: str,
        payload: Any = None,
        route: bool = True,
    ) -> None:
        if self._coordinator is None:
            return
        with contextlib.suppress(Exception):
            self._coordinator.record(
                team_id,
                type=type,
                dedupe_key=dedupe_key,
                actor=actor,
                payload=payload,
                route=route,
            )

    def _kick(self, team_id: str, *, reason: str) -> None:
        if self._coordinator is None:
            return
        with contextlib.suppress(Exception):
            self._coordinator.kick(team_id, reason=reason, need_decision=True)

    def _push_runtime(self, team_id: str) -> None:
        if self._frame_notify is None:
            return
        row = self.runtime(team_id)
        if row is None:
            return
        with contextlib.suppress(Exception):
            self._frame_notify(
                team_id,
                {
                    "type": "team.runtime.updated",
                    "team_id": team_id,
                    "status": row.status,
                    "mode": row.mode,
                    "active_decision_id": row.active_decision_id,
                },
            )

    def _has_work(self, team_id: str) -> bool:
        return self._repos.team_goal_repo.active(team_id) is not None or bool(
            self._repos.team_task_repo.count_open(team_id) > 0
        )

    def _resolve_interval(self, interval: int | None, trigger: str | None) -> int:
        if trigger:
            kind, value = _parse_trigger(trigger)
            if kind == "interval" and value:
                return max(HEARTBEAT_FLOOR_SEC, int(value))
        if interval is not None:
            return max(HEARTBEAT_FLOOR_SEC, int(interval))
        return DEFAULT_HEARTBEAT_SEC

    async def _one_shot_wait(self, team_id: str, trigger: str, now: int) -> None:
        kind, value = _parse_trigger(trigger)
        if kind != "date" or not value:
            return
        key = f"timer:{team_id}:legacy:{value}"
        if self._repos.team_wait_repo.get_by_idempotency_key(key) is not None:
            return
        self._repos.team_wait_repo.create(
            team_id,
            kind=WAIT_KIND_TIMER,
            idempotency_key=key,
            due_at=max(value, now),
            payload={"reason": "legacy_date_drive", "trigger": trigger},
        )

    async def _drop_legacy_drive_jobs(self, team_id: str) -> None:
        cron_manager = getattr(self._agent_manager, "cron_manager", None)
        if cron_manager is None:
            return
        for row in cron_manager.list_by_agent(team_id):
            if is_team_drive_job(row):
                with contextlib.suppress(Exception):
                    await cron_manager.delete(row.cron_id)


__all__ = [
    "DEFAULT_HEARTBEAT_SEC",
    "HEARTBEAT_FLOOR_SEC",
    "NotifyTeam",
    "TeamAutopilot",
]
