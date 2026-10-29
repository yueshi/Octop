"""Team drive loop: the wake cron, its guardrails, and stale-task reaping.

A drive job is a normal cron row wearing ``meta={"kind": "team_drive"}``.
The host creates/disables it through the restricted drive tools; delivery
recognises the marker, filters the wake frame off the room wall, and reaps
tasks whose in-flight dispatch died with the previous process.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from octop.infra.agents.teams.task_state import OPEN_TASK_STATUSES
from octop.infra.cron.trigger import build_trigger
from octop.infra.errors import ErrorCode, OctopError

if TYPE_CHECKING:
    from octop.infra.agents.teams.jobs import TeamJobTracker
    from octop.infra.cron.manager import CronManager
    from octop.infra.db.repos.team_tasks import TeamTaskRepo

logger = logging.getLogger(__name__)

TEAM_DRIVE_KIND = "team_drive"
TEAM_DRIVE_INTERVAL_FLOOR_SEC = 900
TEAM_DRIVE_OPEN_TASK_LIMIT = 10

_ALLOWED_DRIVE_KINDS = frozenset({"interval", "date"})


def is_team_drive_job(row: Any) -> bool:
    meta = getattr(row, "meta", None)
    return isinstance(meta, dict) and meta.get("kind") == TEAM_DRIVE_KIND


def validate_drive_trigger(spec: str) -> None:
    """Drive jobs accept only interval (>= floor) or one-shot date triggers.

    Wall-clock ``cron:`` expressions can fire per-minute and ``agently:`` mail
    events let external content steer the loop, so both stay out of the
    whitelist (external content is data, not a drive source).
    """
    kind = str(spec or "").split(":", 1)[0]
    if kind not in _ALLOWED_DRIVE_KINDS:
        raise OctopError(
            ErrorCode.CRON_TRIGGER_INVALID,
            "team drive triggers must be interval:<seconds> or date:<ISO8601>",
        )
    if kind == "interval":
        try:
            seconds = int(spec.split(":", 1)[1])
        except ValueError as exc:
            raise OctopError(ErrorCode.CRON_TRIGGER_INVALID, str(exc)) from exc
        if seconds < TEAM_DRIVE_INTERVAL_FLOOR_SEC:
            raise OctopError(
                ErrorCode.CRON_TRIGGER_INVALID,
                f"team drive interval must be at least {TEAM_DRIVE_INTERVAL_FLOOR_SEC} seconds",
            )
    # Full parse so malformed specs fail at enable time, not first fire.
    build_trigger(spec)


def wake_frame_text(item: Any, prompt: str) -> bool:
    """Whether a projected history item is this wake's user frame.

    Matched on verbatim content (role user + text equality): the wake prompt is
    delivered as the turn's seed message, so it is identifiable without relying
    on the harness round-tripping message metadata.
    """
    if getattr(item, "role", "") not in ("human", "user"):
        return False
    try:
        wire = json.loads(item.message_json)
    except (ValueError, TypeError, AttributeError):
        return False
    data = wire.get("data") if isinstance(wire, dict) else None
    content = data.get("content") if isinstance(data, dict) else None
    if content is None and isinstance(wire, dict):
        content = wire.get("content")
    if isinstance(content, list):
        content = "".join(
            str(block.get("text") or "") if isinstance(block, dict) else str(block)
            for block in content
        )
    return content == prompt


def reap_stale_tasks(
    *,
    team_id: str,
    task_repo: TeamTaskRepo,
    jobs: TeamJobTracker | None,
    boot_ts: int,
) -> list[str]:
    """Fail ``running`` tasks whose in-flight dispatch lock was lost.

    Any running task last touched before this process booted with a non-busy
    assignee cannot have live work: the in-process roster lock died with the
    previous process. Marked ``failed`` with a stale note so the next wake can
    re-propose and re-dispatch — never ``needs_input`` (nothing is awaiting a
    human decision here).
    """
    busy: set[str] = jobs.busy_member_ids(team_id) if jobs is not None else set()
    reaped: list[str] = []
    for row in task_repo.list_by_team(team_id, status="running"):
        if row.assignee in busy or row.updated_at >= boot_ts:
            continue
        updated = task_repo.update(
            row.task_id,
            expected_version=row.version,
            fields={
                "status": "failed",
                "result_summary": "stale: in-flight dispatch lost on restart",
            },
        )
        if updated is not None:
            reaped.append(row.task_id)
    if reaped:
        logger.info("team %s reaped %d stale running task(s)", team_id, len(reaped))
    return reaped


async def stop_team_drive(
    *,
    team_id: str,
    cron_manager: CronManager | None,
    task_repo: TeamTaskRepo,
    team_inbox: Any = None,
) -> dict[str, int]:
    """Kill switch: disable the drive job, drain the inbox, close open tasks."""
    disabled = 0
    if cron_manager is not None:
        for row in cron_manager.list_by_agent(team_id):
            if is_team_drive_job(row):
                await cron_manager.delete(row.cron_id)
                disabled += 1

    drained = 0
    if team_inbox is not None:
        for msg in team_inbox.list(source=team_id):
            if team_inbox.cancel(msg.id):
                drained += 1

    cancelled = 0
    for task in task_repo.list_by_team(team_id):
        if task.status not in OPEN_TASK_STATUSES:
            continue
        updated = task_repo.update(
            task.task_id,
            expected_version=task.version,
            fields={"status": "cancelled", "blocked_reason": "team drive stopped"},
        )
        if updated is not None:
            cancelled += 1
    return {"disabled_jobs": disabled, "drained_jobs": drained, "cancelled_tasks": cancelled}


__all__ = [
    "TEAM_DRIVE_INTERVAL_FLOOR_SEC",
    "TEAM_DRIVE_KIND",
    "TEAM_DRIVE_OPEN_TASK_LIMIT",
    "is_team_drive_job",
    "reap_stale_tasks",
    "stop_team_drive",
    "validate_drive_trigger",
    "wake_frame_text",
]
