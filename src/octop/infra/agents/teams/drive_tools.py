"""Built-in drive-loop tools mounted on team hosts only.

The host may run exactly one drive cron job whose prompt is locked to the
wake template — arbitrary prompts would let the LLM schedule itself free-form
text every N minutes. Triggers are whitelisted in ``teams.drive``.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from langchain_core.tools import StructuredTool
from langgraph.config import get_config
from pydantic import Field

from octop.infra.agents.tool_scope import current_tool_scope
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.cron import CronJobRepo
from octop.infra.db.repos.team_tasks import TeamTaskRepo
from octop.infra.db.repos.users import UserRepo

DRIVE_ENABLE = "drive_enable"
DRIVE_DISABLE = "drive_disable"
DRIVE_LIST = "drive_list"

TEAM_DRIVE_TOOL_NAMES: tuple[str, ...] = (DRIVE_ENABLE, DRIVE_DISABLE, DRIVE_LIST)


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


def _err(message: str) -> str:
    return _ok({"error": message})


def build_team_drive_tools(
    *,
    cron_manager: Any,
    task_repo: TeamTaskRepo,
    agent_repo: AgentRepo,
    cron_repo: CronJobRepo,
    user_repo: UserRepo,
    team_inbox: Any = None,
    autopilot: Any | None = None,
) -> list[StructuredTool]:
    """Return the restricted drive-loop tools (wired via ``HarnessAgentConfig.tools``).

    ``autopilot`` switches the tools from legacy drive-cron rows to
    ``team_runtime`` (phase 3); tests and offline callers may omit it.
    """

    def _require_host(agent_id: str, user_id: int) -> Any:
        row = agent_repo.get(agent_id)
        if row is None or row.user_id is None or int(row.user_id) != user_id:
            raise ValueError("team drive tools are only available to the team creator")
        return row

    async def drive_enable(
        trigger: Annotated[
            str,
            Field(
                description=(
                    "Drive schedule. interval:<seconds> (min 900) or date:<ISO8601> "
                    "for a single wake. The wake prompt is fixed by the system."
                ),
            ),
        ],
        name: Annotated[
            str | None,
            Field(description="Optional display name; default is the localized drive-loop name."),
        ] = None,
    ) -> str:
        try:
            agent_id, user_id = _tool_ctx()
            _require_host(agent_id, user_id)
            if autopilot is None:
                # The drive loop lives on ``team_runtime`` since v2; there is
                # no legacy cron path to fall back to any more.
                return _err("team runtime is not available")
            await autopilot.start(agent_id, user_id=user_id, trigger=trigger)
            return _ok({"enabled": True, **autopilot.status_payload(agent_id)})
        except Exception as exc:
            return _err(str(exc))

    async def drive_disable() -> str:
        try:
            agent_id, user_id = _tool_ctx()
            _require_host(agent_id, user_id)
            if autopilot is None:
                return _err("team runtime is not available")
            counts = await autopilot.stop(
                agent_id, cron_manager=cron_manager, team_inbox=team_inbox
            )
            return _ok({"stopped": True, **counts})
        except Exception as exc:
            return _err(str(exc))

    async def drive_list() -> str:
        try:
            agent_id, user_id = _tool_ctx()
            _require_host(agent_id, user_id)
            if autopilot is None:
                return _err("team runtime is not available")
            payload = autopilot.status_payload(agent_id)
            payload["enabled"] = payload["status"] in (
                "running",
                "deciding",
                "dispatching",
                "waiting",
            )
            return _ok(payload)
        except Exception as exc:
            return _err(str(exc))

    return [
        StructuredTool.from_function(
            coroutine=drive_enable,
            name=DRIVE_ENABLE,
            description=(
                "Enable this team's autonomous drive loop: a scheduled wake that reviews "
                "open tasks and keeps work moving. One drive job per team; the wake prompt "
                "is fixed (review tasks, dispatch, report) and cannot be customized."
            ),
        ),
        StructuredTool.from_function(
            coroutine=drive_disable,
            name=DRIVE_DISABLE,
            description=(
                "Full stop for the autonomous drive loop (kill switch): disables the drive "
                "job, drains queued member dispatches, and cancels all open tasks."
            ),
        ),
        StructuredTool.from_function(
            coroutine=drive_list,
            name=DRIVE_LIST,
            description="Show the team's drive job and whether the loop is enabled.",
        ),
    ]


__all__ = [
    "DRIVE_DISABLE",
    "DRIVE_ENABLE",
    "DRIVE_LIST",
    "TEAM_DRIVE_TOOL_NAMES",
    "build_team_drive_tools",
]
