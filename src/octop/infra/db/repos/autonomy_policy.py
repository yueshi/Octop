"""Autonomy policy — the job spec behind a digital employee.

The columns live on ``proactive_care_config`` (migration 001, extended by 022)
because the duty window and cadence are the same values proactive care
already schedules on: one row per agent, one place to read. ``Agents`` keeps
the on/off switch (``autonomy_enabled``) — presence of a policy row means
"configured", not "running".
"""

from __future__ import annotations

from dataclasses import dataclass

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import now_ts

_POLICY_COLUMNS = (
    "active_hours_start",
    "active_hours_end",
    "min_interval_hours",
    "max_interval_hours",
    "budget_limit_tokens",
    "budget_window",
    "sla_tasks_per_day",
    "escalate_to",
    "responsible_scope",
    "tool_scope_allow",
    "tool_scope_deny",
)


@dataclass(frozen=True)
class AutonomyPolicy:
    """Job parameters for one digital employee.

    ``budget_limit_tokens == 0`` and ``sla_tasks_per_day == 0`` mean "no
    limit". ``responsible_scope`` and ``escalate_to`` are consumed when the
    autonomy prompt is assembled (M5); until then they are stored and
    returned verbatim.
    """

    agent_id: str
    active_hours_start: str = "09:00"
    active_hours_end: str = "22:00"
    min_interval_hours: int = 5
    max_interval_hours: int = 24
    budget_limit_tokens: int = 0
    budget_window: str = "day"
    sla_tasks_per_day: int = 0
    escalate_to: str = ""
    responsible_scope: str = ""
    tool_scope_allow: str = ""
    tool_scope_deny: str = ""


class AutonomyPolicyRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def get_optional(self, agent_id: str) -> AutonomyPolicy | None:
        """The stored policy, or ``None`` when the agent has none.

        ``get`` returns defaults, which would make every agent look
        configured — callers that must not change behaviour for agents
        without a job spec need this distinction.
        """
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM proactive_care_config WHERE agent_id = ?", (agent_id,)
            ).fetchone()
        return self.get(agent_id) if row is not None else None

    def get(self, agent_id: str) -> AutonomyPolicy:
        """Read the policy, falling back to defaults for an unconfigured agent."""
        columns = ", ".join(_POLICY_COLUMNS)
        with self._db.connect() as conn:
            row = conn.execute(
                f"SELECT {columns} FROM proactive_care_config WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
        if row is None:
            return AutonomyPolicy(agent_id=agent_id)
        return AutonomyPolicy(
            agent_id=agent_id,
            active_hours_start=str(row[0] or "09:00"),
            active_hours_end=str(row[1] or "22:00"),
            min_interval_hours=int(row[2] or 5),
            max_interval_hours=int(row[3] or 24),
            budget_limit_tokens=int(row[4] or 0),
            budget_window=str(row[5] or "day"),
            sla_tasks_per_day=int(row[6] or 0),
            escalate_to=str(row[7] or ""),
            responsible_scope=str(row[8] or ""),
            tool_scope_allow=str(row[9] or ""),
            tool_scope_deny=str(row[10] or ""),
        )

    def upsert(self, policy: AutonomyPolicy) -> None:
        """Write the job parameters, leaving ``enabled`` (care push) untouched."""
        columns = ", ".join(_POLICY_COLUMNS)
        placeholders = ", ".join("?" for _ in _POLICY_COLUMNS)
        updates = ", ".join(f"{name} = excluded.{name}" for name in _POLICY_COLUMNS)
        values = tuple(getattr(policy, name) for name in _POLICY_COLUMNS)
        with self._db.transaction() as conn:
            conn.execute(
                # ``enabled`` (care push) is left to the table default: a job
                # spec must not silently turn proactive care on or off.
                f"INSERT INTO proactive_care_config (agent_id, {columns}, updated_at) "
                f"VALUES (?, {placeholders}, ?) "
                f"ON CONFLICT(agent_id) DO UPDATE SET {updates}",
                (policy.agent_id, *values, now_ts()),
            )
